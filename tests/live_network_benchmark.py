"""Exercise live routing over loopback TLS with synthetic network and desktop events.

Run with PYTHONPATH set to the checkout being measured. The same script can
exercise a different revision without copying its files into the checkout.
Each request has half the artificial RTT before and after the real TLS call.
Optional jitter varies that RTT. Optional bandwidth charges time for each
base64 image payload in transit, with no shared-link contention model.
With --wire-delay, delay both directions of TCP (including TLS handshakes)
before running the viewer in polling or --stream mode.
The fixture has a JPEG-like payload but uses no display, encoder, or OS input.
"""
import argparse
import asyncio
import base64
from collections import Counter
from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import random
import secrets
import ssl
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace

from lanpilot.live_agent import LiveAgent
from lanpilot.live_view import LiveView
from lanpilot.peer_client import PinnedHub, atomic_json
from lanpilot.peer_gateway import PeerGateway


GEOMETRY = '0,0,1280,720'
CAPTURE_PERIOD = .120
WORKER_PERIOD = .015


class DelayedHub(PinnedHub):
    def __init__(self, endpoint, credential, options, requests, seed):
        super().__init__(endpoint, credential)
        self.options = options
        self.requests = requests
        self.randomizer = random.Random(seed)

    def transfer_time(self, data):
        if self.options.mbps and data and data.get('frame'):
            return len(data['frame'].get('jpeg_base64', '')) * 8 / (self.options.mbps * 1_000_000)
        return 0

    def call(self, operation, body=None):
        self.requests.append((time.monotonic(), operation))
        jitter = self.randomizer.uniform(-self.options.jitter_ms, self.options.jitter_ms)
        half_rtt = max(0, self.options.rtt_ms + jitter) / 2000
        outbound = half_rtt + self.transfer_time(body)
        if outbound:
            time.sleep(outbound)
        value = None
        try:
            value = super().call(operation, body)
            return value
        finally:
            inbound = half_rtt + self.transfer_time(value)
            if inbound:
                time.sleep(inbound)


class TimedView(LiveView):
    def __init__(self, hub, identifier, stop, arrivals, stream_version):
        super().__init__(hub, identifier, stop, stream_version=stream_version)
        self.arrivals_log = arrivals
        self.last_sequence = 0

    def publish(self, value, roundtrip=0):
        super().publish(value, roundtrip)
        frame = value.get('frame')
        if frame and frame['seq'] > self.last_sequence:
            self.last_sequence = frame['seq']
            self.arrivals_log.append((time.monotonic(), self.last_sequence))


def percentiles(values):
    ordered = sorted(values)
    if not ordered:
        raise ValueError('No samples in measurement window')
    return {name: round(1000 * ordered[math.ceil(len(ordered) * fraction) - 1], 1)
            for name, fraction in [('p50', .50), ('p95', .95)]}


def benchmark(gateway, endpoint, workspace, options):
    source_token = secrets.token_urlsafe(32)
    viewer_token = secrets.token_urlsafe(32)
    requests, connections = [], []

    def factory(address, credential):
        hub = DelayedHub(address, credential, options, requests, seed=len(connections))
        connections.append(hub)
        return hub

    # The fixture uses the normal registration, pairing and authorization path.
    dispatch = gateway.hub.dispatch
    target = dispatch('register', source_token, {'name': 'Synthetic source', 'platform': 'windows', 'live_version': 1})
    dispatch('register', viewer_token, {'name': 'Synthetic viewer', 'platform': 'macos'})
    dispatch('availability', source_token, {'enabled': True})
    code = dispatch('code', source_token, {})['code']
    approval = dispatch('connect', viewer_token, {'code': code})['approval']
    dispatch('approve', source_token, {'approval': approval, 'allow': True})

    deliveries, sent_inputs, applied_inputs, captured = [], {}, {}, {}
    stop = threading.Event()
    quiet = threading.Event()
    controller = factory(endpoint, viewer_token)
    client = SimpleNamespace(config={'endpoint': endpoint, 'credential': source_token},
                             agent=SimpleNamespace(workspace=workspace), incoming=True, stop_event=stop)
    agent = LiveAgent(client, factory, atomic_json)
    opened = time.monotonic()
    session = controller.call('live-open', {'peer': target['id']})
    identifier = session['id']
    view = TimedView(factory(endpoint, viewer_token), identifier, stop, deliveries,
                     session['view_stream'] if options.stream else None)
    image = b'\xff\xd8' + bytes(options.jpeg_kib * 1024 - 4) + b'\xff\xd9'
    encoded = base64.b64encode(image).decode()

    def worker():
        frame = ack = 0
        next_capture = time.monotonic()
        while not stop.is_set():
            try:
                lease = json.loads((workspace / 'live-control.json').read_text(encoding='utf-8'))
            except FileNotFoundError:
                lease = {}
            if lease.get('id') != identifier or lease['expires'] <= time.time():
                stop.wait(WORKER_PERIOD)
                continue
            for path in sorted(workspace.glob('*.live-input.json')):
                batch = json.loads(path.read_text(encoding='utf-8'))
                ack = batch['seq']
                applied_inputs[ack] = time.monotonic()
                atomic_json(workspace / 'live-receipt.json', {'id': identifier, 'seq': ack})
                path.unlink()
            now = time.monotonic()
            if not quiet.is_set() and now >= next_capture:
                frame += 1
                captured[frame] = (now, ack)
                payload = encoded if options.repeat_image else base64.b64encode(
                    image[:2] + frame.to_bytes(8, 'big') + image[10:]).decode()
                atomic_json(workspace / 'live-frame.json', {'id': identifier, 'seq': frame,
                            'geometry': GEOMETRY, 'created': time.time(), 'jpeg_base64': payload})
                next_capture += CAPTURE_PERIOD
                if next_capture <= now:
                    next_capture = now + CAPTURE_PERIOD
            stop.wait(WORKER_PERIOD)

    workers = [threading.Thread(target=callback, daemon=True) for callback in (agent.run, agent.frames, worker)]
    view.start()
    for thread in workers:
        thread.start()
    try:
        while not deliveries and time.monotonic() - opened < 5:
            stop.wait(.01)
        if not deliveries:
            raise RuntimeError(f'No first frame: {agent.error or view.snapshot()["error"]}')
        first_frame = deliveries[0][0] - opened
        active_start = time.monotonic()
        active_end = active_start + options.active_seconds
        randomizer = random.Random(42)
        next_input = active_start + .2
        while time.monotonic() < active_end:
            now = time.monotonic()
            snapshot = view.snapshot()
            if snapshot['error'] or not snapshot['frame']:
                raise RuntimeError(snapshot['error'] or 'Frame became stale during the active run')
            if next_input <= now < active_end - 1:
                sequence = len(sent_inputs) + 1
                sent_inputs[sequence] = now
                controller.call('live-input', {'id': identifier, 'seq': sequence, 'geometry': GEOMETRY,
                                'events': [{'type': event, 'button': 0, 'x': 100, 'y': 200}
                                           for event in ('down', 'up')]})
                next_input += .50 + randomizer.uniform(0, .3)
            stop.wait(.01)

        quiet.set()
        # Allow the final media request to drain before counting idle polls.
        stop.wait(.60)
        idle_start = time.monotonic()
        idle_end = idle_start + options.idle_seconds
        while time.monotonic() < idle_end:
            snapshot = view.snapshot()
            if snapshot['error']:
                raise RuntimeError(snapshot['error'])
            stop.wait(.05)
        if options.stream and any(op == 'live-view' for _, op in requests):
            raise RuntimeError('Streaming viewer fell back to polling')
    finally:
        stop.set()
        view.close()
        view.thread.join(timeout=3)
        for thread in workers:
            thread.join(timeout=3)
        for hub in connections:
            if hub.connection:
                hub.connection.close()

    active_deliveries = [(at, seq) for at, seq in deliveries if active_start <= at < active_end]
    visible_inputs = {}
    for at, sequence in deliveries:
        _, ack = captured[sequence]
        for input_sequence in range(1, ack + 1):
            visible_inputs.setdefault(input_sequence, at)
    if not sent_inputs or sent_inputs.keys() != applied_inputs.keys() or sent_inputs.keys() != visible_inputs.keys():
        raise RuntimeError('One or more synthetic actions had no observed effect')
    idle_polls = Counter(op for at, op in requests if idle_start <= at < idle_end)
    return {
        'rtt_ms_per_peer': options.rtt_ms,
        'delay_at_tcp': options.wire_delay,
        'viewer_mode': 'stream' if options.stream else 'poll',
        'rtt_jitter_ms': options.jitter_ms,
        'frame_link_mbps': options.mbps or None,
        'synthetic_jpeg_kib': options.jpeg_kib,
        'active_seconds': options.active_seconds,
        'idle_seconds': options.idle_seconds,
        'first_frame_ms': round(1000 * first_frame, 1),
        'delivered_frames': len(active_deliveries),
        'delivered_fps': round(len(active_deliveries) / options.active_seconds, 2),
        'capture_to_cache_ms': percentiles(at - captured[seq][0] for at, seq in active_deliveries),
        'synthetic_input_count': len(sent_inputs),
        'input_to_worker_ms': percentiles(applied_inputs[seq] - at for seq, at in sent_inputs.items()),
        'input_to_changed_frame_cache_ms': percentiles(visible_inputs[seq] - at for seq, at in sent_inputs.items()),
        'idle_view_requests_per_s': round(idle_polls['live-view'] / options.idle_seconds, 2),
        'idle_exchange_requests_per_s': round(idle_polls['live-exchange'] / options.idle_seconds, 2),
    }


@contextmanager
def delayed_tcp(port, rtt_ms):
    """Delay bytes without decrypting TLS or adding a sleep per frame/chunk."""
    loop = asyncio.new_event_loop()
    delay = rtt_ms / 2000
    handlers = set()

    async def pump(reader, writer):
        pending = asyncio.Queue(maxsize=32)

        async def read():
            while data := await reader.read(32768):
                await pending.put((loop.time() + delay, data))
            await pending.put((loop.time() + delay, b''))

        async def write():
            while True:
                when, data = await pending.get()
                await asyncio.sleep(max(0, when - loop.time()))
                if not data:
                    writer.write_eof()
                    return
                writer.write(data)
                await writer.drain()

        tasks = [asyncio.create_task(job()) for job in (read, write)]
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def forward(reader, writer):
        handler = asyncio.current_task()
        handlers.add(handler)
        remote = None
        tasks = []
        try:
            source, remote = await asyncio.open_connection('127.0.0.1', port)
            tasks = [asyncio.create_task(pump(source, writer)), asyncio.create_task(pump(reader, remote))]
            await asyncio.gather(*tasks)
        except OSError:
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for connection in (writer, remote):
                if connection:
                    connection.close()
            handlers.discard(handler)

    server = loop.run_until_complete(asyncio.start_server(forward, '127.0.0.1', 0))
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        async def shutdown():
            server.close()
            await server.wait_closed()
            for handler in tuple(handlers):
                handler.cancel()
            await asyncio.gather(*handlers, return_exceptions=True)

        asyncio.run_coroutine_threadsafe(shutdown(), loop).result(3)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=3)
        loop.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repeat-image', action='store_true', help='Use identical image bytes for each capture')
    parser.add_argument('--rtt-ms', type=float, default=100)
    parser.add_argument('--wire-delay', action='store_true', help='Delay TCP bytes in both directions')
    parser.add_argument('--stream', action='store_true', help='Use the negotiated HTTPS media stream')
    parser.add_argument('--jitter-ms', type=float, default=0)
    parser.add_argument('--mbps', type=float, default=0, help='Artificial frame bandwidth, or 0 for unlimited')
    parser.add_argument('--jpeg-kib', type=int, default=96)
    parser.add_argument('--active-seconds', type=float, default=10)
    parser.add_argument('--idle-seconds', type=float, default=4)
    options = parser.parse_args()
    if not 0 <= options.rtt_ms <= 400 or not 1 <= options.jpeg_kib <= 1000:
        parser.error('RTT must be 0–400 ms and JPEG size 1–1000 KiB')
    if not 0 <= options.jitter_ms <= 400 or not 0 <= options.mbps < math.inf:
        parser.error('Jitter must be 0–400 ms and bandwidth must be a finite nonnegative value')
    if not 3 <= options.active_seconds <= 120 or not 1 <= options.idle_seconds <= 6:
        parser.error('Active window must be 3–120 s and idle window 1–6 s')
    if options.wire_delay and (options.jitter_ms or options.mbps):
        parser.error('TCP delay uses fixed latency without bandwidth or jitter simulation')
    if options.stream and not options.wire_delay:
        parser.error('Streaming requires delay at the TCP layer')

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        cert, key = root / 'cert.pem', root / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-subj', '/CN=localhost', '-keyout', str(key), '-out', str(cert)],
                       check=True, capture_output=True)
        pin = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text(encoding='ascii'))).hexdigest()
        with PeerGateway(root, cert, key, 0) as gateway:
            gateway_thread = threading.Thread(target=gateway.serve_forever, daemon=True)
            gateway_thread.start()
            try:
                endpoint = {'url': f'https://127.0.0.1:{gateway.server_port}', 'pin': pin}
                if options.wire_delay:
                    with delayed_tcp(gateway.server_port, options.rtt_ms) as port:
                        simulated = argparse.Namespace(**vars(options))
                        simulated.rtt_ms = 0  # The raw TCP forwarding path supplies all delay.
                        result = benchmark(gateway, dict(endpoint, url=f'https://127.0.0.1:{port}'), root, simulated)
                        result['rtt_ms_per_peer'] = options.rtt_ms
                else:
                    result = benchmark(gateway, endpoint, root, options)
            finally:
                gateway.shutdown()
                gateway_thread.join(timeout=2)
                gateway.hub.close()
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
