"""Compare 918911e with the working tree, using loopback TLS and a synthetic mailbox."""
import argparse
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import pickle
import subprocess
import sys
import tempfile
import threading

_SOURCE = Path(__file__).resolve().parents[2]
_PRIOR = _SOURCE.parent / 'viewer-startup-0703f5e'
sys.path.insert(0, str(_PRIOR))
import wan_measure as fixture


@asynccontextmanager
async def shape_downlink(target, port_ready, mbps, rtt_ms):
    clients = set()
    delay = rtt_ms / 2000
    bytes_per_second = mbps * 1e6 / 8

    async def pump(reader, writer, rate):
        # Schedule propagation separately from reading and serialization so
        # the artificial RTT does not also cap throughput to one chunk/RTT.
        loop = asyncio.get_running_loop()
        queue = asyncio.Queue(maxsize=32)

        async def schedule():
            last = 0
            while chunk := await reader.read(4096):
                last = max(last, loop.time()) + (len(chunk) / rate if rate else 0)
                await queue.put((last + delay, chunk))
            await queue.put((last + delay, b''))

        producer = asyncio.create_task(schedule())
        try:
            while True:
                due, chunk = await queue.get()
                await asyncio.sleep(max(0, due - loop.time()))
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
        finally:
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)

    async def connect(reader, writer):
        task = asyncio.current_task()
        clients.add(task)
        upstream = None
        try:
            source, upstream = await asyncio.open_connection('127.0.0.1', target, limit=16384)
            legs = [asyncio.create_task(pump(reader, upstream, 0)),
                    asyncio.create_task(pump(source, writer, bytes_per_second))]
            try:
                await asyncio.wait(legs, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for leg in legs:
                    leg.cancel()
                await asyncio.gather(*legs, return_exceptions=True)
        finally:
            writer.close()
            if upstream:
                upstream.close()
            clients.discard(task)

    async with await asyncio.start_server(connect, '127.0.0.1', 0, limit=16384) as server:
        port_ready.append(server.sockets[0].getsockname()[1])
        try:
            yield
        finally:
            for client in list(clients):
                client.cancel()
            await asyncio.gather(*clients, return_exceptions=True)


def run_trial(root, source, label, args):
    sys.path[:0] = [str(source), str(source / 'tests/video_stream')]
    from lanpilot.live import AvcHistory, LiveSessions
    from lanpilot.peer_client import PinnedHub
    from lanpilot.peer_gateway import Handler, PeerGateway

    relay_events = []
    begin, add = LiveSessions.begin_stream, AvcHistory.add_decoded
    next_frame = getattr(AvcHistory, 'fresh_frame', AvcHistory.next_frame)
    chunk = Handler.stream_chunk

    def record_begin(self, credential, identifier, accept_avc):
        session = begin(self, credential, identifier, accept_avc)
        relay_events.append({'kind': 'subscribe', 't_ms': fixture.wall(), 'avc': bool(session.get('avc'))})
        return session

    def record_add(self, record, now):
        seq = add(self, record, now)
        if seq is not None:
            relay_events.append({'kind': 'accepted', 't_ms': fixture.wall(), 'seq': seq, 'idr': record[0]['idr']})
        return seq

    def record_select(self, after, now):
        requested = self.need_idr
        frame, captured = next_frame(self, after, now)
        if not requested and self.need_idr:
            relay_events.append({'kind': 'request_idr', 't_ms': fixture.wall(), 'after': after})
        return frame, captured

    def record_chunk(self, raw):
        started = fixture.wall()
        chunk(self, raw)
        if len(raw) > 1:
            value = json.loads(raw)
            frame = value.get('frame')
            relay_events.append({'kind': 'write', 't_ms': started, 'end_ms': fixture.wall(),
                                 'bytes': len(raw), 'seq': frame['seq'] if frame else None,
                                 'frame_age_ms': value['frame_age_ms']})

    Handler.stream_chunk = record_chunk
    LiveSessions.begin_stream = record_begin
    AvcHistory.add_decoded = record_add
    setattr(AvcHistory, next_frame.__name__, record_select)
    gateway = PeerGateway(root / label, root / 'relay.crt', root / 'relay.key', 0)
    relay_thread = threading.Thread(target=gateway.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
    relay_thread.start()
    port_ready, stop = [], threading.Event()

    async def forward():
        async with shape_downlink(gateway.server_port, port_ready, args.mbps, args.rtt_ms):
            await asyncio.to_thread(stop.wait)

    proxy = threading.Thread(target=lambda: asyncio.run(forward()), daemon=True)
    try:
        direct = fixture.hub_endpoint(gateway.server_port, args.pin)
        endpoint = direct
        if 'slow' in label:
            proxy.start()
            endpoint = fixture.hub_endpoint(fixture.wait_for(lambda: port_ready and port_ready[0]), args.pin)
        probe = fixture.probe_route(PinnedHub, endpoint, 3)
        with (root / 'fixture.pkl').open('rb') as file:
            units = pickle.load(file)
        result = fixture.trial(root / (label + '-view'), root / 'wk', source / 'linko_desktop.py',
                               (direct, endpoint), args.seconds, units, label, args.hold_local_media_ms)
        result.update(relay_events=relay_events, route_probe=probe)
        subscriptions = [e for e in relay_events if e['kind'] == 'subscribe']
        summary = result['summary']
        summary.update(subscriptions=len(subscriptions), accepted=sum(e['kind'] == 'accepted' for e in relay_events),
                       media_write_bytes=sum(e['bytes'] for e in relay_events if e['kind'] == 'write' and e['seq']),
                       idr_requests=sum(e['kind'] == 'request_idr' for e in relay_events),
                       subscription_gap_ms=fixture.ms_data(b['t_ms'] - a['t_ms'] for a, b in
                                                           zip(subscriptions, subscriptions[1:])))
        print(json.dumps(result, ensure_ascii=False))
    finally:
        stop.set()
        if proxy.is_alive():
            proxy.join(3)
        gateway.shutdown()
        relay_thread.join()
        gateway.server_close()
        gateway.hub.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, default=18)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--mbps', type=float, default=1.6)
    parser.add_argument('--rtt-ms', type=int, default=600)
    parser.add_argument('--hold-local-media-ms', type=int, default=0)
    parser.add_argument('--modes', nargs='+', choices=('direct', 'slow'), default=['direct', 'slow'])
    parser.add_argument('--output', type=Path, default=Path('downlink-results.json'))
    parser.add_argument('--worker', nargs=3, help=argparse.SUPPRESS)
    parser.add_argument('--pin', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        root, source, label = args.worker
        run_trial(Path(root), Path(source), label, args)
        return
    with tempfile.TemporaryDirectory(prefix='lanpilot-downlink-') as tmp:
        root = Path(tmp)
        baseline = root / 'baseline'
        baseline.mkdir()
        archive = subprocess.check_output(['git', 'archive', '918911e'], cwd=_SOURCE)
        subprocess.run(['tar', '-xf', '-', '-C', str(baseline)], input=archive, check=True)
        fixture.check(fixture.quiet_run(['swiftc', str(_PRIOR / 'wan_viewer.swift'), '-o', str(root / 'wk')]),
                      'Offscreen WebKit harness build')
        (root / 'loopback.sb').write_text('(version 1)\n(allow default)\n(deny network*)\n'
                                         '(allow network-bind (local ip "localhost:*"))\n'
                                         '(allow network-inbound (local ip "localhost:*"))\n'
                                         '(allow network-outbound (remote ip "localhost:*"))\n')
        cert, key = root / 'relay.crt', root / 'relay.key'
        fixture.check(fixture.quiet_run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt',
                                        'ec_paramgen_curve:prime256v1', '-nodes', '-subj', '/CN=Loopback study',
                                        '-days', '1', '-keyout', str(key), '-out', str(cert)]), 'TLS certificate')
        pin = hashlib.sha256(subprocess.check_output(['openssl', 'x509', '-in', str(cert), '-outform', 'DER'])).hexdigest()
        sys.path.insert(0, str(_SOURCE / 'tests/video_stream'))
        units, metadata = fixture.preencode('bounded')
        with (root / 'fixture.pkl').open('wb') as file:
            pickle.dump(units, file)
        sources = {'baseline': baseline, 'bounded': _SOURCE}
        files = ['lanpilot/live.py', 'lanpilot/live_view.py', 'lanpilot/peer_client.py',
                 'lanpilot/peer_gateway.py', 'ui/app.js']
        report = {'seconds': args.seconds, 'downlink_mbps': args.mbps, 'rtt_ms': args.rtt_ms,
                  'hold_local_media_ms': args.hold_local_media_ms, 'fixture': metadata,
                  'sha256': {variant: {file: hashlib.sha256((path / file).read_bytes()).hexdigest()
                                       for file in files} for variant, path in sources.items()}, 'trials': []}
        for repeat in range(args.repeats):
            for mode in args.modes[::1 if repeat % 2 == 0 else -1]:
                for variant in list(sources)[::1 if repeat % 2 == 0 else -1]:
                    label = f'{mode}-{repeat+1}-{variant}'
                    (root / label).mkdir()
                    result = subprocess.run(
                        [sys.executable, __file__, '--worker',
                         str(root), str(sources[variant]), label, '--pin', pin, '--seconds', str(args.seconds),
                         '--mbps', str(args.mbps), '--rtt-ms', str(args.rtt_ms),
                         '--hold-local-media-ms', str(args.hold_local_media_ms)],
                        capture_output=True, text=True, timeout=args.seconds + 45,
                        env=dict(os.environ, PYTHONPATH=str(sources[variant])))
                    if result.returncode != 0:
                        raise RuntimeError(f'{label}: {result.stderr}')
                    trial = json.loads(result.stdout)
                    report['trials'].append(trial)
                    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
                    print(json.dumps(trial['summary'], ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
