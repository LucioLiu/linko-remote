"""Compare product AVC upload modes using a fixed-cadence synthetic Windows mailbox."""

import argparse
from collections import Counter
from contextlib import ExitStack, nullcontext
import hashlib
import json
from pathlib import Path
import platform
import secrets
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

from serial_avc_tls import FPS, GOP, RTTS, preencode, p95_ms

ROOT = Path(__file__).resolve().parents[2]
sys.path[1:1] = [str(ROOT), str(ROOT / 'tests')]

from lanpilot.live import AVC_BATCH_FRAMES, AVC_HEADER
from lanpilot.live_agent import LiveAgent
from lanpilot.peer_client import PinnedHub, atomic_json
from lanpilot.peer_gateway import Handler, PeerGateway
from live_network_benchmark import delayed_tcp


def tick_ms():
    return time.monotonic_ns() // 1_000_000


def close_hub(hub):
    if hub.connection is not None:
        hub.connection.close()


def trial(home, cert, key, pin, records, rtt, mode, seconds, input_desktop=False):
    uploads = []
    captures, arrivals = {}, {}
    condition = threading.Condition()
    ready = threading.Event()
    stop = threading.Event()
    errors = []

    class Uploader(PinnedHub):
        def call(self, operation, body=None):
            if operation not in ('live-frame', 'live-avc-batch'):
                return super().call(operation, body)
            count = len(body['frames']) if operation == 'live-avc-batch' else 1
            started = time.monotonic()
            value = super().call(operation, body)
            uploads.append((count, time.monotonic() - started))
            return value

    class RelayHandler(Handler):
        def answer(self, status, response):
            if mode != 'stream' and response.get('ok'):
                value = response['value']
                value.pop('avc_upload', None)
                for session in value.get('sessions', []):
                    session.pop('avc_upload', None)
            if mode == 'serial' and response.get('ok'):
                value = response['value']
                value.pop('avc_batch', None)
                for session in value.get('sessions', []):
                    session.pop('avc_batch', None)
            super().answer(status, response)

    home.mkdir()
    with ExitStack() as stack:
        relay = stack.enter_context(PeerGateway(home, cert, key, 0))
        relay.RequestHandlerClass = RelayHandler
        stack.callback(relay.hub.close)
        server = threading.Thread(target=relay.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
        server.start()
        stack.callback(server.join, 2)
        stack.callback(relay.shutdown)
        proxy = delayed_tcp(relay.server_port, rtt) if rtt else nullcontext(relay.server_port)
        sender_port = stack.enter_context(proxy)
        viewer_endpoint = {'url': f'https://127.0.0.1:{relay.server_port}', 'pin': pin}
        sender_endpoint = dict(viewer_endpoint, url=f'https://127.0.0.1:{sender_port}')
        viewer = PinnedHub(viewer_endpoint, secrets.token_urlsafe(32))
        sender = PinnedHub(sender_endpoint, secrets.token_urlsafe(32))
        client = SimpleNamespace(config={'endpoint': sender_endpoint, 'credential': sender.credential},
                                 stop_event=stop, incoming=True, agent=SimpleNamespace(workspace=home))
        agent = LiveAgent(client, Uploader, atomic_json)
        agent.avc_enabled = True
        for hub in (viewer, sender, agent.hub, agent.frame_hub):
            stack.callback(close_hub, hub)
        viewer.call('register', {'name': 'Loopback viewer', 'platform': 'macos'})
        target = sender.call('register', {'name': 'Loopback sender', 'platform': 'windows',
                                         'live_version': 1, 'live_avc': 1})['id']
        sender.call('availability', {'enabled': True})
        code = sender.call('code')['code']
        approval = viewer.call('connect', {'code': code})['approval']
        sender.call('approve', {'approval': approval, 'allow': True})
        identifier = viewer.call('live-open', {'peer': target, 'accept_avc': 1})['id']
        stream = viewer.live_stream(identifier, accept_avc=True)

        desktop = None
        if input_desktop:
            from input_avc_tls import InputDesktop
            desktop = InputDesktop(viewer, agent, identifier, AVC_HEADER, atomic_json)

        after = None

        def publish(value, _):
            nonlocal after
            ready.set()
            if value['error']:
                raise RuntimeError(value['error'])
            frame = value['frame']
            if desktop:
                desktop.publish(frame, value['ack'])
            if not frame:
                if not value.get('unchanged'):
                    after = None
                return
            seq = frame['seq']
            if ((not desktop and frame['avc_base64'] != records[seq - 1]) or frame['geometry'] != '0,0,1280,720'
                    or (not frame['idr'] and (after is None or seq != after + 1))):
                raise ValueError(f'Invalid arrival at frame {seq}')
            with condition:
                arrivals.setdefault(seq, time.monotonic())
                condition.notify_all()
            after = seq

        def receive():
            try:
                if not stream.run(lambda: not stop.is_set(), publish) and not stop.is_set():
                    raise RuntimeError('Viewer stream ended')
            except Exception as exc:
                errors.append(str(exc))

        agent.frame_hub.call('live-agent-state')  # Warm both media paths equally.
        stack.enter_context(patch('lanpilot.live_agent.windows_tick_ms', tick_ms))
        workers = [threading.Thread(target=entry, daemon=True) for entry in (receive, agent.run, agent.frames)]

        def finish():
            stop.set()
            stream.abort()
            for worker in workers:
                worker.join(4)
            if any(worker.is_alive() for worker in workers):
                raise RuntimeError('Trial workers did not stop')

        stack.callback(finish)
        for worker in workers:
            worker.start()
        if not ready.wait(4):
            raise RuntimeError(f'Viewer did not start: {errors}')
        deadline = time.monotonic() + 4
        while not (control := agent.read('live-control.json')).get('live_avc'):
            if time.monotonic() > deadline:
                raise RuntimeError(f'AVC lease did not start: {agent.error}')
            time.sleep(.01)
        batch = control.get('avc_batch') == 1
        if batch != (mode != 'serial') or getattr(agent, 'avc_stream', False) != (mode == 'stream'):
            raise RuntimeError('Unexpected upload negotiation')

        start = time.monotonic()
        end = start + seconds
        if desktop:
            clicks = threading.Thread(target=desktop.clicks, args=(end, errors), daemon=True)
            clicks.start()
            workers.append(clicks)
        fault = (seconds // 2) * FPS + 5
        for seq, record in enumerate(records, 1):
            due = start + (seq - 1) / FPS
            time.sleep(max(0, due - time.monotonic()))
            captured = time.monotonic()
            captures[seq] = captured
            if desktop:
                record = desktop.capture(seq, captured)
            if seq in (fault, fault + 1):
                continue
            mailbox = f'live-avc-{seq % AVC_BATCH_FRAMES}.json' if batch else 'live-frame.json'
            atomic_json(home / mailbox, {'id': identifier, 'seq': seq, 'avc_base64': record,
                                        'capture_tick': int(captured * 1000), 'idr': (seq - 1) % GOP == 0})
        if desktop:
            clicks.join(2)
        with condition:
            condition.wait_for(lambda: len(records) in arrivals or errors, timeout=.8)
            observed = dict(arrivals)
        if mode == 'stream' and (not agent.avc_stream or uploads):
            raise RuntimeError('Candidate fell back during the measured window')
        if errors or agent.error:
            raise RuntimeError(f'Viewer {errors}, sender {agent.error}')
        if not observed:
            raise RuntimeError('No media arrived')

        in_window = sum(start <= arrival < end for arrival in observed.values())
        recovered = min((seq for seq in observed if seq > fault + 1), default=None)
        missing = len(captures.keys() - observed.keys())
        return {
            **(desktop.results() if desktop else {}),
            'mode': mode, 'sender_rtt_ms': rtt, 'received_in_window': in_window,
            'arrival_fps': round(in_window / seconds, 3), 'received_unique': len(observed),
            'arrival_latency_p95_ms': p95_ms([arrived - captures[seq] for seq, arrived in observed.items()]),
            'missing_after_drain': missing, 'missing_excluding_injected': missing - 2,
            'recovery_from_first_dropped_frame_ms': round(1000 * (observed[recovered] - captures[fault]), 3)
            if recovered is not None else None,
            'first_recovered_seq': recovered, 'idr_requests': agent.avc_key,
            'upload_acks': len(uploads), 'upload_size_distribution': dict(Counter(count for count, _ in uploads)),
            'upload_ack_p95_ms': p95_ms([elapsed for _, elapsed in uploads]),
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, default=15)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--rtts', nargs='+', type=int, default=RTTS)
    parser.add_argument('--modes', nargs='+', choices=('serial', 'batch', 'stream'), default=('serial', 'batch'))
    parser.add_argument('--input', action='store_true', help='Encode a synthetic desktop that responds to real protocol clicks')
    args = parser.parse_args()
    if args.seconds < 4 or args.repeats < 1:
        parser.error('Use at least 4 seconds and 1 repeat')
    if args.input:
        records = [None] * (FPS * args.seconds)
        fixture = {'frames': len(records), 'source': 'Scrolling text with a click-driven pixel marker'}
    else:
        records, fixture = preencode(FPS * args.seconds, AVC_HEADER)
    with tempfile.TemporaryDirectory(prefix='lanpilot-batch-avc-') as temp:
        home = Path(temp)
        cert, key = home / 'cert.pem', home / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-subj', '/CN=localhost', '-keyout', str(key), '-out', str(cert)],
                       check=True, capture_output=True)
        pin = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text('ascii'))).hexdigest()
        trials = []
        for repeat in range(args.repeats):
            order = args.rtts[repeat % len(args.rtts):] + args.rtts[:repeat % len(args.rtts)]
            for index, rtt in enumerate(order):
                modes = args.modes if (repeat + index) % 2 == 0 else reversed(args.modes)
                for mode in modes:
                    print(f'Trial {repeat + 1}/{args.repeats}, {rtt} ms, {mode}', file=sys.stderr, flush=True)
                    result = trial(home / f'{repeat}-{rtt}-{mode}', cert, key, pin, records, rtt, mode, args.seconds, args.input)
                    trials.append({'repeat': repeat + 1, **result})
        print(json.dumps({
            'platform': platform.platform(), 'python': platform.python_version(),
            'seconds_per_trial': args.seconds, 'fixture': fixture, 'fps': FPS, 'gop_frames': GOP,
            'measurement': {
                'transport': 'Pinned TLS with sender-only symmetric byte delay from serial_avc_tls; viewer direct loopback',
                **({'input': 'Monotonic before live-input POST to queued response, synthetic target applying the '
                             'mailbox click, execution receipt at viewer, and first decoded frame with matching pixel '
                             'marker. Receipt and pixels are independently timed. No display presentation.'}
                   if args.input else {}),
                'source': ('Click-responsive synthetic desktop encoded at run time' if args.input
                           else 'Same preencoded moving frames, 20 fps independent of ACK, through product LiveAgent file mailbox'),
                'serial': 'Legacy sender negotiation, with one latest-frame mailbox',
                'batch': 'Negotiated bounded mailbox ring and AVC batch uploads',
                'stream': 'Same mailbox ring and one pinned request with independently reported progress',
                'loss': 'Absent source sequences after drain. Exactly two source slots omitted midway in each trial',
                'recovery': 'First arrival after the injected gap, measured from first omitted capture slot. '
                            'Fixture uses scheduled IDRs rather than synthesizing one when the sender requests it',
            },
            'trials': trials,
        }, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
