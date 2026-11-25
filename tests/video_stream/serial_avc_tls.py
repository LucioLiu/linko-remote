"""Measure one-in-flight AVC uploads against the committed loopback TLS relay.

Run from this worktree with Python 3.12+ and video_stream/requirements.txt.
No working-tree product code or configured relay/device credentials are used.
"""

import argparse
import base64
from collections import Counter
from contextlib import ExitStack, nullcontext
from fractions import Fraction
import hashlib
import io
import json
import math
from pathlib import Path
import platform
import secrets
import socket
import ssl
import subprocess
import sys
import tarfile
import tempfile
import threading
import time

import av


ROOT = Path(__file__).resolve().parents[2]
REVISION = 'b12512e41da57f356a1ee3ff08e238e8ed5e2d01'
WIDTH, HEIGHT, FPS = 1280, 720, 20
GOP = FPS
RTTS = (0, 100, 200)


def load_committed_code(destination):
    paths = ('desktop.py', 'lanpilot', 'tests/live_network_benchmark.py', 'tests/video_stream/benchmark.py',
             'tests/video_stream/jpeg_delta.py')
    snapshot = subprocess.check_output(['git', 'archive', REVISION, *paths], cwd=ROOT)
    with tarfile.open(fileobj=io.BytesIO(snapshot)) as archive:
        archive.extractall(destination, filter='data')
    sys.path[:0] = [str(destination), str(destination / 'tests'),
                    str(destination / 'tests/video_stream')]


def preencode(count, header):
    from benchmark import ScrollingText

    scene = ScrollingText(WIDTH, HEIGHT, 'full')
    encoder = av.CodecContext.create('libx264', 'w')
    encoder.width, encoder.height = WIDTH, HEIGHT
    encoder.time_base = Fraction(1, FPS)
    encoder.framerate = FPS
    encoder.pix_fmt = 'yuv420p'
    encoder.thread_count = 1
    encoder.gop_size = GOP
    encoder.max_b_frames = 0
    encoder.options = {'profile': 'baseline', 'preset': 'veryfast', 'tune': 'zerolatency',
                       'crf': '26', 'x264-params': 'annexb=1:repeat-headers=1:scenecut=0:open-gop=0'}
    encoder.open()
    decoder = av.CodecContext.create('h264', 'r')
    decoder.thread_count = 1
    records = []
    sizes = []
    last_pixels = None
    digest = hashlib.sha256()
    for index in range(count):
        frame = av.VideoFrame.from_ndarray(scene.frame(index), format='rgb24')
        frame = frame.reformat(format='yuv420p')
        frame.pts = index
        packet, = encoder.encode(frame)
        if packet.pts != index or packet.dts != index or packet.is_keyframe != (index % GOP == 0):
            raise ValueError(f'Unexpected AVC frame order or GOP at frame {index}')
        decoded, = decoder.decode(packet)
        pixels = hashlib.sha256(decoded.to_ndarray(format='gray')).digest()
        if (decoded.width, decoded.height) != (WIDTH, HEIGHT) or pixels == last_pixels:
            raise ValueError(f'Fixture did not produce changing 720p pictures at frame {index}')
        last_pixels = pixels
        payload = bytes(packet)
        digest.update(payload)
        sizes.append(len(payload))
        # This is the original fixture clock in milliseconds. Only its order is
        # used on the wire. Actual trial timing uses the monotonic clock below.
        record = header.pack(b'LPA1', len(payload), index + 1, index * 1000 // FPS,
                             WIDTH, HEIGHT, int(packet.is_keyframe), 0, 0, WIDTH, HEIGHT)
        records.append(base64.b64encode(record + payload).decode('ascii'))
    if encoder.encode(None) or decoder.decode(None):
        raise ValueError('Fixture codec delayed output')
    return records, {'frames': count, 'annex_b_bytes': sum(sizes), 'min_frame_bytes': min(sizes),
                     'max_frame_bytes': max(sizes), 'sha256': digest.hexdigest()}


def p95_ms(seconds):
    ordered = sorted(seconds)
    return round(1000 * ordered[math.ceil(len(ordered) * .95) - 1], 3) if ordered else None


def run_trial(home, cert, key, pin, records, rtt, seconds):
    from lanpilot.live import LIVE_AVC_STREAM_RECORD_MAX, LIVE_STREAM_SECONDS, LIVE_STREAM_TYPE
    from lanpilot.peer_client import PinnedConnection, PinnedHub
    from lanpilot.peer_gateway import PeerGateway
    from live_network_benchmark import delayed_tcp

    class UploadHub(PinnedHub):
        def __init__(self, endpoint):
            super().__init__(endpoint, secrets.token_urlsafe(32))
            self.connections = 0
            self.calls = 0

        def _connect(self):
            super()._connect()
            self.connections += 1

        def _call(self, operation, body=None):
            if operation == 'live-frame':
                self.calls += 1
            return super()._call(operation, body)

    def close_hub(hub):
        if hub.connection is not None:
            hub.connection.close()

    home.mkdir()
    with ExitStack() as stack:
        gateway = stack.enter_context(PeerGateway(home, cert, key, 0))
        stack.callback(gateway.hub.close)
        relay_thread = threading.Thread(target=gateway.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
        relay_thread.start()
        stack.callback(relay_thread.join, 2)
        stack.callback(gateway.shutdown)
        proxy = delayed_tcp(gateway.server_port, rtt) if rtt else nullcontext(gateway.server_port)
        sender_port = stack.enter_context(proxy)
        endpoint = {'url': f'https://127.0.0.1:{gateway.server_port}', 'pin': pin}
        viewer = PinnedHub(endpoint, secrets.token_urlsafe(32))
        sender = UploadHub(dict(endpoint, url=f'https://127.0.0.1:{sender_port}'))
        stack.callback(close_hub, viewer)
        stack.callback(close_hub, sender)
        viewer.call('register', {'name': 'Loopback viewer', 'platform': 'macos'})
        target = sender.call('register', {'name': 'Loopback sender', 'platform': 'windows',
                                          'live_version': 1, 'live_avc': 1})['id']
        sender.call('availability', {'enabled': True})
        code = sender.call('code')['code']
        approval = viewer.call('connect', {'code': code})['approval']
        sender.call('approve', {'approval': approval, 'allow': True})
        session = viewer.call('live-open', {'peer': target, 'accept_avc': 1})
        identifier = session['id']
        expected = {'id': identifier, 'live_avc': 1, 'need_idr': True}
        if session.get('view_stream') != 1 or session.get('live_avc') != 1:
            raise RuntimeError('Relay did not negotiate AVC streaming')

        arrivals = {}
        duplicates = 0
        batches = 0
        receiver_errors = []
        ready = threading.Event()
        stop = threading.Event()
        received = threading.Condition()
        connection = PinnedConnection(viewer.host, viewer.port, viewer.pin)
        connection.connect()
        sock = connection.sock
        sock.settimeout(LIVE_STREAM_SECONDS + 2)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        headers = {'Authorization': 'Bearer ' + viewer.credential, 'Content-Type': 'application/json'}

        def receive():
            nonlocal batches, duplicates
            after = None
            try:
                while not stop.is_set():
                    if connection.sock is not sock:
                        raise ConnectionError('Viewer TLS connection closed before batch renewal')
                    body = json.dumps({'id': identifier, 'after': after, 'accept_avc': 1})
                    connection.request('POST', '/v2/live-stream', body, headers)
                    with connection.getresponse() as response:
                        if (response.status != 200 or not response.chunked
                                or response.getheader('Content-Type') != LIVE_STREAM_TYPE):
                            raise ConnectionError(f'Unexpected live stream response: HTTP {response.status}')
                        batches += 1
                        ready.set()
                        while not stop.is_set():
                            raw = response.readline(LIVE_AVC_STREAM_RECORD_MAX + 1)
                            if not raw:
                                break
                            if raw == b'\n':
                                continue
                            if len(raw) > LIVE_AVC_STREAM_RECORD_MAX or not raw.endswith(b'\n'):
                                raise ValueError('Invalid live stream record')
                            update = json.loads(raw)
                            arrived = time.monotonic()
                            if update['id'] != identifier or update['error']:
                                raise ValueError(f'Invalid stream session or relay error: {update["error"]}')
                            frame = update['frame']
                            if frame is None:
                                if not update.get('unchanged'):
                                    after = None
                                continue
                            seq = frame['seq']
                            if not 1 <= seq <= len(records) or frame.get('avc_base64') != records[seq - 1]:
                                raise ValueError(f'Unexpected AVC media at frame {seq}')
                            if not frame['idr'] and (after is None or seq != after + 1):
                                raise ValueError(f'Broken AVC reference chain at frame {seq}')
                            with received:
                                if seq in arrivals:
                                    duplicates += 1
                                else:
                                    arrivals[seq] = arrived
                                received.notify_all()
                            after = seq
            except Exception as error:
                with received:
                    if not stop.is_set():
                        receiver_errors.append(f'{type(error).__name__}: {error}')
                        received.notify_all()
                ready.set()

        thread = threading.Thread(target=receive, daemon=True)

        def finish_receiver():
            stop.set()
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            thread.join(6)
            connection.close()
            if thread.is_alive():
                raise RuntimeError('Viewer did not stop')

        thread.start()
        stack.callback(finish_receiver)
        if not ready.wait(6) or receiver_errors:
            raise RuntimeError(f'Viewer failed to start: {receiver_errors}')
        if expected not in sender.call('live-agent-state')['sessions']:
            raise RuntimeError('Sender did not see negotiated AVC session')
        initial_connections = sender.connections
        attempts = {}
        accepted = set()
        acks = []
        reply_states = Counter()
        sender_errors = []
        start = time.monotonic()
        end = start + seconds
        due = start
        period = 1 / FPS
        for seq, record in enumerate(records, 1):
            if due >= end or receiver_errors:
                break
            time.sleep(max(0, due - time.monotonic()))
            started = time.monotonic()
            if started >= end:
                break
            # Keep the target cadence across timer jitter. If an ack misses a
            # full slot, re-anchor without generating a queue of older frames.
            if started - due >= period:
                due = started
            due += period
            attempts[seq] = started
            try:
                ack = sender.call('live-frame', {'id': identifier, 'frame': {
                    'avc_base64': record, 'capture_age_ms': 0}})
            except Exception as error:
                sender_errors.append(f'{type(error).__name__}: {error}')
                break
            acks.append(time.monotonic() - started)
            if ack['frame_seq'] == seq:
                accepted.add(seq)
            else:
                reply_states['unaccepted'] += 1
            if ack.get('need_idr'):
                reply_states['need_idr'] += 1
            if not ack.get('active') or not ack.get('live_avc'):
                reply_states['inactive_or_downgraded'] += 1
                break

        with received:
            received.wait_for(lambda: accepted.issubset(arrivals) or receiver_errors, timeout=2)
            observed = sorted(arrivals.values())
            latency = [arrival - attempts[seq] for seq, arrival in arrivals.items() if seq in attempts]
            gaps = [right - left for left, right in zip(observed, observed[1:])]
            in_window = sum(start <= arrival < end for arrival in observed)
            return {
                'sender_rtt_ms': rtt, 'window_seconds': seconds, 'target_fps': FPS,
                'target_frame_slots': FPS * seconds, 'uploads_started': len(attempts),
                'acks_received': len(acks), 'relay_accepted': len(accepted),
                'received_in_window': in_window, 'arrival_fps': round(in_window / seconds, 3),
                'received_after_window': len(arrivals) - in_window, 'received_unique': len(arrivals),
                'accepted_missing_after_drain': len(accepted - arrivals.keys()),
                'attempted_missing_after_drain': len(attempts.keys() - arrivals.keys()),
                'receiver_duplicates': duplicates, 'arrival_latency_p95_ms': p95_ms(latency),
                'upload_ack_p95_ms': p95_ms(acks), 'arrival_gap_p95_ms': p95_ms(gaps),
                'arrival_gap_max_ms': round(1000 * max(gaps), 3) if gaps else None,
                'upload_retries': sender.calls - len(attempts),
                'upload_tls_reconnects': sender.connections - initial_connections,
                'viewer_stream_batches': batches, 'sender_errors': sender_errors,
                'receiver_errors': receiver_errors.copy(), 'other_replies': dict(reply_states),
            }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, default=15, help='Measured seconds per trial')
    parser.add_argument('--repeats', type=int, default=3, help='Trials per RTT')
    args = parser.parse_args()
    if args.seconds < 4 or args.repeats < 1:
        parser.error('Use at least 4 seconds and 1 repeat')
    with tempfile.TemporaryDirectory(prefix='lanpilot-serial-avc-') as temp:
        root = Path(temp)
        load_committed_code(root)
        from lanpilot.live import AVC_HEADER

        records, fixture = preencode(FPS * args.seconds, AVC_HEADER)
        cert, key = root / 'cert.pem', root / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-subj', '/CN=localhost', '-keyout', str(key), '-out', str(cert)],
                       check=True, capture_output=True)
        pin = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text('ascii'))).hexdigest()
        trials = []
        for repeat in range(args.repeats):
            order = RTTS[repeat % len(RTTS):] + RTTS[:repeat % len(RTTS)]
            for rtt in order:
                print(f'Trial {repeat + 1}/{args.repeats}, sender RTT {rtt} ms', file=sys.stderr, flush=True)
                trial = run_trial(root / f'{repeat}-{rtt}', cert, key, pin, records, rtt, args.seconds)
                trials.append({'repeat': repeat + 1, **trial})
        report = {'revision': REVISION, 'platform': platform.platform(), 'python': platform.python_version(),
                  'pyav': av.__version__, 'ffmpeg_libraries': av.library_versions,
                  'measurement': {
                      'topology': 'Only the sender uses the TLS byte proxy (RTT/2 each way); viewer is direct loopback',
                      'source': 'Release the next contiguous pre-encoded frame at up to 20 fps after the prior ack',
                      'latency': 'Sender release to viewer receipt and parsing of the AVC NDJSON record, same monotonic clock',
                      'loss': 'Attempted or relay-accepted sequence numbers absent after the receive drain',
                  },
                  'fixture': {'width': WIDTH, 'height': HEIGHT, 'fps_ceiling': FPS,
                              'idr_interval_frames': GOP, **fixture}, 'trials': trials}
        print(json.dumps(report, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
