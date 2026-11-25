"""Compare two pinned LanPilot revisions over loopback TLS with independent device pairs."""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import platform
import resource
import secrets
import ssl
import statistics
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
BASE = '918911e88e6e956f5295ecf42b3bda7ce172f6ef'
BOUNDED = '1ebc3318637076671c06322fd431fdfd4a2f62b9'
FPS = 20
GOP = FPS
GEOMETRY = '0,0,1280,720'
WARMUP = 2
DRAIN = 1


def lower_priority():
    adjustment = max(0, 10 - os.nice(0))
    return os.nice(adjustment)


def output_json(value):
    print(json.dumps(value, ensure_ascii=False, allow_nan=False), flush=True)


def wait_until(due):
    time.sleep(max(0, due - time.monotonic()))


def ms(seconds):
    return round(1000 * seconds, 3)


def distribution(values):
    values = sorted(values)
    if not values:
        return {'n': 0, 'p50': None, 'p95': None, 'max': None}
    return {'n': len(values), 'p50': round(statistics.median(values), 3),
            'p95': round(values[math.ceil(len(values) * .95) - 1], 3),
            'max': round(values[-1], 3)}


def archive(revision, destination):
    raw = subprocess.check_output(['git', 'archive', revision, 'desktop.py', 'lanpilot'], cwd=ROOT)
    with tarfile.open(fileobj=io.BytesIO(raw)) as bundle:
        bundle.extractall(destination, filter='data')


def relay_worker(source, home, cert, key):
    sys.path.insert(0, str(source))
    from lanpilot.peer_gateway import PeerGateway

    with PeerGateway(home, cert, key, 0, bind='127.0.0.1') as relay:
        thread = threading.Thread(target=relay.serve_forever, kwargs={'poll_interval': .05})
        thread.start()
        try:
            output_json({'port': relay.server_port, 'nice': os.nice(0)})
            for command in sys.stdin:
                if command.strip() != 'sample':
                    break
                usage = resource.getrusage(resource.RUSAGE_SELF)
                output_json({'at': time.monotonic(), 'cpu_s': usage.ru_utime + usage.ru_stime,
                             'peak_rss_bytes': usage.ru_maxrss * (1 if sys.platform == 'darwin' else 1024)})
        finally:
            relay.shutdown()
            thread.join()
            relay.hub.close()


def worker_command(*args):
    return [sys.executable, '-B', str(Path(__file__).resolve()), *map(str, args)]


def start_relay(stack, source, home, cert, key):
    home.mkdir()
    relay = stack.enter_context(subprocess.Popen(worker_command('--relay', source, home, cert, key),
                                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True))

    def stop():
        try:
            relay.stdin.close()
            relay.wait(timeout=5)
        except subprocess.TimeoutExpired:
            relay.kill()
            relay.wait()

    stack.callback(stop)
    return relay, json.loads(relay.stdout.readline())


def relay_sample(relay):
    relay.stdin.write('sample\n')
    relay.stdin.flush()
    sample = json.loads(relay.stdout.readline())
    rss_kib = subprocess.check_output(['ps', '-o', 'rss=', '-p', str(relay.pid)], text=True)
    sample['rss_bytes'] = int(rss_kib.strip()) * 1024
    sample['host_load_1m'] = os.getloadavg()[0]
    return sample


def wan_process_count():
    rows = subprocess.check_output(['ps', '-axww', '-o', 'args='])
    commands = [row.split() for row in rows.splitlines()]
    return sum(os.path.basename(command[0]).lower().startswith(b'python')
               and any(os.path.basename(arg) == b'wan_measure.py' for arg in command[1:])
               for command in commands if command)


def close_hub(hub):
    if hub.connection is not None:
        hub.connection.close()


def click_events(seq):
    return [{'type': kind, 'x': 512 * seq, 'y': 512, 'button': 0} for kind in ('down', 'up')]


def windows_tick():
    return time.monotonic_ns() // 1_000_000


def stop_pair(pair):
    pair.stop.set()
    pair.view.close()
    for thread in pair.threads:
        thread.join(timeout=5)
        if thread.is_alive():
            raise RuntimeError(f'Worker did not stop: {thread.name}')


class Pair:
    def __init__(self, index, endpoint, mailbox, records, stack, bounded):
        from lanpilot.live_agent import LiveAgent
        from lanpilot.live_view import LiveView
        from lanpilot.peer_client import PinnedHub, atomic_json

        self.index, self.mailbox, self.records = index, mailbox, records
        self.stop = threading.Event()
        self.captures, self.received, self.inputs = {}, {}, {}
        self.subscriptions, self.fallbacks, self.errors = [], [], []
        self.stream_attempts = 0
        self.ack = 0
        self.last_seq = None
        self.duplicate = 0
        mailbox.mkdir()

        pair = self

        class ObservedHub(PinnedHub):
            def live_stream(self, *args, **kwargs):
                pair.stream_attempts += 1
                stream = super().live_stream(*args, **kwargs)
                getresponse = stream.connection.getresponse

                def observed_response():
                    response = getresponse()
                    pair.subscriptions.append((time.monotonic(), response.status))
                    return response

                stream.connection.getresponse = observed_response
                return stream

            def call(self, operation, body=None):
                if operation == 'live-view':
                    pair.fallbacks.append(time.monotonic())
                return super().call(operation, body)

        class ObservedView(LiveView):
            def publish(self, value, roundtrip=0):
                now = time.monotonic()
                super().publish(value, roundtrip)
                pair.observe(value, now, roundtrip)

        self.viewer = ObservedHub(endpoint, secrets.token_urlsafe(32))
        sender = PinnedHub(endpoint, secrets.token_urlsafe(32))
        self.viewer_device = self.viewer.call('register', {'name': f'Viewer {index}', 'platform': 'macos'})['id']
        self.sender_device = sender.call('register', {'name': f'Sender {index}', 'platform': 'windows',
                                                      'live_version': 1, 'live_avc': 1})['id']
        stack.callback(close_hub, sender)
        stack.callback(close_hub, self.viewer)
        sender.call('availability', {'enabled': True})
        code = sender.call('code')['code']
        approval = self.viewer.call('connect', {'code': code})['approval']
        sender.call('approve', {'approval': approval, 'allow': True})
        session = self.viewer.call('live-open', {'peer': self.sender_device, 'accept_avc': 1, 'avc_view_batch': 1})
        self.ident = session['id']
        if session.get('view_stream') != 1 or session.get('live_avc') != 1:
            raise RuntimeError('AVC downlink did not negotiate')
        if (session.get('avc_view_batch') == 1) != bounded:
            raise RuntimeError('Unexpected bounded downlink negotiation')

        client = SimpleNamespace(config={'endpoint': endpoint, 'credential': sender.credential},
                                 stop_event=self.stop, incoming=True, agent=SimpleNamespace(workspace=mailbox))
        self.write = atomic_json
        self.agent = LiveAgent(client, PinnedHub, atomic_json)
        self.agent.avc_enabled = True
        for hub in (self.agent.hub, self.agent.frame_hub):
            stack.callback(close_hub, hub)
        options = {'bounded': True} if bounded else {}
        self.view = ObservedView(self.viewer, self.ident, self.stop, stream_version=1, avc=True, **options)
        self.threads = [threading.Thread(target=target, daemon=True, name=f'{name}-{index}')
                        for name, target in [('control', self.agent.run), ('upload', self.agent.frames)]]
        stack.callback(stop_pair, self)
        for thread in self.threads:
            thread.start()
        self.view.start()
        self.threads.append(self.view.thread)

    def observe(self, value, now, roundtrip):
        if value['error']:
            self.errors.append(value['error'])
            return
        for seq in range(self.ack + 1, value['ack'] + 1):
            if seq in self.inputs:
                self.inputs[seq].setdefault('ack', now)
            else:
                self.errors.append(f'Unexpected input receipt {seq}')
        self.ack = max(self.ack, value['ack'])
        frame = value.get('frame')
        if frame is None:
            if not value.get('unchanged'):
                self.last_seq = None
            return
        seq = frame['seq']
        if (seq not in self.captures or frame['geometry'] != GEOMETRY
                or frame.get('avc_base64') != self.records[seq - 1]
                or frame['idr'] != ((seq - 1) % GOP == 0)):
            self.errors.append(f'Unexpected AVC record {seq}')
            return
        if seq in self.received:
            self.duplicate += 1
            return
        if not frame['idr'] and (self.last_seq is None or seq != self.last_seq + 1):
            self.errors.append(f'AVC reference discontinuity at {seq}')
            self.last_seq = None
            return
        self.last_seq = seq
        self.received[seq] = (now, round(value.get('frame_age_ms', 0) + 1000 * roundtrip, 3))

    def produce(self, source_start):
        from lanpilot.live import AVC_BATCH_FRAMES

        for seq, record in enumerate(self.records, 1):
            if self.stop.wait(max(0, source_start + (seq - 1) / FPS - time.monotonic())):
                return
            captured = time.monotonic()
            self.captures[seq] = captured
            self.write(self.mailbox / f'live-avc-{seq % AVC_BATCH_FRAMES}.json',
                       {'id': self.ident, 'seq': seq, 'avc_base64': record,
                        'capture_tick': int(captured * 1000), 'idr': (seq - 1) % GOP == 0})

    def submit(self, start, end):
        for seq, offset in enumerate(range(1, int(end - start), 2), 1):
            if self.stop.wait(max(0, start + offset - time.monotonic())):
                return
            sample = self.inputs[seq] = {'submitted': time.monotonic()}
            try:
                result = self.viewer.call('live-input', {'id': self.ident, 'seq': seq, 'geometry': GEOMETRY,
                                                         'events': click_events(seq)})
                if result.get('accepted') != seq:
                    raise ValueError('Wrong queued sequence')
                sample['queued'] = time.monotonic()
            except Exception as error:
                sample['error'] = str(error)
                break  # Never send a sequence past an uncertain submission.

    def apply_inputs(self):
        seq = 1
        while not self.stop.wait(.005):
            event = self.agent.read(f'{self.ident}-{seq:010d}.live-input.json')
            if not event:
                continue
            if (event.get('id') != self.ident or event.get('seq') != seq or seq not in self.inputs
                    or event.get('events') != click_events(seq) or event.get('geometry') != GEOMETRY
                    or event.get('expires', 0) <= time.time()):
                self.errors.append(f'Invalid or expired synthetic input {seq}')
                return
            self.inputs[seq]['applied'] = time.monotonic()
            self.write(self.mailbox / 'live-receipt.json', {'id': self.ident, 'seq': seq})
            seq += 1

    def summary(self, origin, start, end):
        received = self.received.copy()
        captures = self.captures.copy()
        observed = {seq: (at, estimate) for seq, (at, estimate) in received.items() if start <= at < end}
        ages = [ms(at - captures[seq]) for seq, (at, _) in observed.items()]
        offsets = sorted(at for at, _ in observed.values())
        gaps = [ms(b - a) for a, b in zip(offsets, offsets[1:])]
        responses = [(at, status) for at, status in self.subscriptions if start <= at < end]
        renewals = sum(index > 0 and status == 200 and start <= at < end
                       for index, (at, status) in enumerate(self.subscriptions))
        samples = []
        for seq, data in self.inputs.items():
            submitted = data['submitted']
            samples.append({'seq': seq, 'submitted_offset_ms': ms(submitted - start),
                            **{f'{key}_ms': ms(data[key] - submitted) if key in data else None
                               for key in ('queued', 'applied', 'ack')},
                            'error': data.get('error')})
        planned = round((end - start) * FPS)
        measure_slots = range(WARMUP * FPS + 1, WARMUP * FPS + planned + 1)
        complete = self.view.thread.is_alive() and not self.view.stop.is_set() and self.agent.current == self.ident
        errors = self.errors.copy()
        if self.agent.error:
            errors.append(self.agent.error)
        return {
            'pair': self.index, 'successful': bool(complete and not errors and observed and not self.fallbacks),
            'disconnected': not complete, 'fallback_polls': len(self.fallbacks), 'errors': errors,
            'upload_stream_active': self.agent.avc_stream, 'upload_last_confirmed_seq': self.agent.avc_seq,
            'capture_slots': planned, 'captured_slots': sum(seq in captures for seq in measure_slots),
            'first_received_ms': ms(min(at for at, _ in received.values()) - origin) if received else None,
            'max_gap_including_window_edges_ms': max(gaps + [ms(offsets[0] - start), ms(end - offsets[-1])])
            if offsets else ms(end - start),
            'received_in_window': len(observed), 'receive_fps': round(len(observed) / (end - start), 3),
            'capture_slots_missing_after_drain': sum(seq not in received for seq in measure_slots),
            'duplicate_frames': self.duplicate, 'capture_to_client_ms': distribution(ages),
            'protocol_estimated_frame_age_ms': distribution(estimate for _, estimate in observed.values()),
            'receive_gap_ms': distribution(gaps),
            'http_stream_attempts': self.stream_attempts,
            'http_responses_in_window': len(responses), 'http_renewals_in_window': renewals,
            'renewals_per_second': round(renewals / (end - start), 3),
            'http_non_200': [status for _, status in self.subscriptions if status != 200],
            'input_planned': len(range(1, int(end - start), 2)), 'inputs': samples,
            'input_ack_ms': distribution(item['ack_ms'] for item in samples if item['ack_ms'] is not None),
            'frames': [{'seq': seq, 'capture_offset_ms': ms(captures[seq] - start),
                        'received_offset_ms': ms(at - start), 'estimated_age_ms': estimate}
                       for seq, (at, estimate) in observed.items()],
        }


def run_trial(args):
    from unittest.mock import patch

    source, home, cert, key, fixture, pin, revision, pairs_arg, seconds_arg = args
    source, home = Path(source), Path(home)
    sys.path.insert(0, str(source))
    records = json.loads(Path(fixture).read_text())
    count, seconds = int(pairs_arg), int(seconds_arg)
    host_samples = []
    with ExitStack() as stack:
        home.mkdir()
        relay, identity = start_relay(stack, source, home / 'relay', cert, key)
        endpoint = {'url': f'https://127.0.0.1:{identity["port"]}', 'pin': pin}
        stack.enter_context(patch('lanpilot.live_agent.windows_tick_ms', windows_tick))
        pairs = [Pair(i, endpoint, home / f'pair-{i}', records, stack, revision == BOUNDED)
                 for i in range(1, count + 1)]
        devices = {device for pair in pairs for device in (pair.viewer_device, pair.sender_device)}
        if len(devices) != 2 * count or len({pair.ident for pair in pairs}) != count:
            raise RuntimeError('Trial requires independent authorized pairs')
        deadline = time.monotonic() + 5
        while not all(pair.subscriptions and pair.agent.avc_stream and pair.agent.avc_active for pair in pairs):
            if time.monotonic() >= deadline:
                raise RuntimeError('AVC leases did not initialize')
            time.sleep(.01)

        origin = time.monotonic() + .1
        start = origin + WARMUP
        end = start + seconds
        for pair in pairs:
            for name, target, params in [('capture', pair.produce, (origin,)),
                                         ('input', pair.submit, (start, end)), ('apply', pair.apply_inputs, ())]:
                thread = threading.Thread(target=target, args=params, daemon=True, name=f'{name}-{pair.index}')
                thread.start()
                pair.threads.append(thread)
        for offset in range(seconds + 1):
            wait_until(start + offset)
            sample = relay_sample(relay)
            sample['offset_ms'] = ms(sample.pop('at') - start)
            sample['wan_measure_processes'] = wan_process_count()
            host_samples.append(sample)
            for pair in pairs:
                pair.view.ui_seen = time.monotonic()  # Keep the product viewer lease alive, as its UI does.
        wait_until(end + DRAIN)
        first, last = host_samples[0], host_samples[-1]
        elapsed = (last['offset_ms'] - first['offset_ms']) / 1000
        cpu = last['cpu_s'] - first['cpu_s']
        return {'revision': revision, 'pairs': count,
                'started_utc': datetime.fromtimestamp(time.time() - (time.monotonic() - start),
                                                      timezone.utc).isoformat(),
                'nice': {'clients': os.nice(0), 'relay': identity['nice']},
                'relay': {'measured_s': round(elapsed, 4), 'cpu_s': round(cpu, 6),
                          'cpu_percent_of_one_core': round(100 * cpu / elapsed, 3),
                          'rss_mib': distribution(sample['rss_bytes'] / 2**20 for sample in host_samples),
                          'peak_rss_mib_since_startup': round(last['peak_rss_bytes'] / 2**20, 3),
                          'samples': host_samples},
                'sessions': [pair.summary(origin, start, end) for pair in pairs]}


def matrix(args):
    import av
    from serial_avc_tls import preencode

    if args.seconds < 6 or args.repeats < 2:
        raise ValueError('Use at least six seconds and two repeats')
    with tempfile.TemporaryDirectory(prefix='lanpilot-multisession-') as tmp:
        home = Path(tmp)
        sources = {revision: home / revision for revision in (BASE, BOUNDED)}
        for revision, location in sources.items():
            archive(revision, location)
        cert, key = home / 'cert.pem', home / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1',
                        '-nodes', '-days', '1', '-subj', '/CN=LanPilot loopback fixture', '-keyout', str(key),
                        '-out', str(cert)], check=True, capture_output=True)
        pin = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest()
        sys.path.insert(0, str(sources[BASE]))
        from lanpilot.live import AVC_HEADER

        records, metadata = preencode((args.seconds + WARMUP + DRAIN) * FPS, AVC_HEADER)
        fixture = home / 'avc.json'
        fixture.write_text(json.dumps(records))
        report = {'platform': platform.platform(), 'hardware_model': subprocess.check_output(
                    ['sysctl', '-n', 'hw.model'], text=True).strip() if sys.platform == 'darwin' else platform.machine(),
                  'logical_cpus': os.cpu_count(), 'python': platform.python_version(), 'pyav': av.__version__,
                  'fixture': {**metadata, 'fps': FPS, 'gop': GOP, 'geometry': GEOMETRY},
                  'warmup_s': WARMUP, 'measurement_s': args.seconds, 'drain_s': DRAIN,
                  'transport': '127.0.0.1 pinned TLS, one relay process per trial',
                  'revisions': {label: revision for label, revision in [('baseline', BASE), ('bounded', BOUNDED)]},
                  'frame_metric': 'Unique, fixture-verified AVC frames with usable reference chain at LiveView.publish. '
                                  'Capture age uses common host monotonic clock. No viewer decode or canvas draw.',
                  'input_metric': 'Synthetic target validates the mailbox click and writes the product receipt. '
                                  'ACK measured at the viewer. No OS input or visual verification.',
                  'trials': []}
        for repeat in range(1, args.repeats + 1):
            counts = args.counts if repeat % 2 else list(reversed(args.counts))
            revisions = (BASE, BOUNDED) if repeat % 2 else (BOUNDED, BASE)
            for count in counts:
                for revision in revisions:
                    label = f'r{repeat}-{count}-{revision[:7]}'
                    trial = subprocess.run(
                        worker_command('--trial', sources[revision], home / label, cert, key, fixture, pin,
                                       revision, count, args.seconds), capture_output=True, text=True,
                        timeout=args.seconds + 35)
                    if trial.returncode or 'Traceback' in trial.stderr:
                        raise RuntimeError(f'{label}: {trial.stderr}')
                    result = json.loads(trial.stdout)
                    result['worker_diagnostics'] = trial.stderr.splitlines()
                    report['trials'].append({'round': repeat, **result})
                    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
                    print(label, 'fps', [s['receive_fps'] for s in result['sessions']],
                          'cpu', result['relay']['cpu_percent_of_one_core'],
                          'WAN overlap samples', sum(s['wan_measure_processes'] > 0
                                                     for s in result['relay']['samples']), flush=True)


def main():
    lower_priority()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, default=12)
    parser.add_argument('--repeats', type=int, default=2)
    parser.add_argument('--counts', nargs='+', type=int, choices=(1, 2, 4), default=[1, 2, 4])
    parser.add_argument('--output', type=Path, default=ROOT / 'tests/video_stream/multisession_tls_results.json')
    parser.add_argument('--trial', nargs=9, help=argparse.SUPPRESS)
    parser.add_argument('--relay', nargs=4, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.relay:
        relay_worker(*map(Path, args.relay))
    elif args.trial:
        output_json(run_trial(args.trial))
    else:
        matrix(args)


if __name__ == '__main__':
    main()
