"""Compare AVC downlink revisions and encoding profiles over loopback TLS."""
import argparse
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import pickle
import platform
from datetime import datetime, timezone
import subprocess
import sys
import tempfile
import threading

_SOURCE = Path(__file__).resolve().parents[2]
import downlink_fixture as fixture


@asynccontextmanager
async def shape_downlink(target, port_ready, downlink_mbps, rtt_ms):
    clients = set()
    delay = rtt_ms / 2000

    async def pump(reader, writer, downstream):
        # Schedule propagation separately from reading and serialization so
        # the artificial RTT does not also cap throughput to one chunk/RTT.
        loop = asyncio.get_running_loop()
        queue = asyncio.Queue(maxsize=32)

        async def schedule():
            last = 0
            while chunk := await reader.read(4096):
                rate = downlink_mbps() * 1e6 / 8 if downstream else 0
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
            legs = [asyncio.create_task(pump(reader, upstream, False)),
                    asyncio.create_task(pump(source, writer, True))]
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
    load_before = os.getloadavg()
    changed_rate = False
    recovery_at = None

    def current_rate():
        nonlocal changed_rate
        if recovery_at is not None and fixture.wall() >= recovery_at:
            if not changed_rate:
                relay_events.append({'kind': 'downlink_recovered', 't_ms': fixture.wall(),
                                     'mbps': args.recover_mbps})
                changed_rate = True
            return args.recover_mbps
        return args.mbps
    begin, add = LiveSessions.begin_stream, AvcHistory.add_decoded
    next_frame = getattr(AvcHistory, 'fresh_frame', AvcHistory.next_frame)
    chunk = Handler.stream_chunk

    def record_begin(self, credential, identifier, accept_avc, **options):
        nonlocal recovery_at
        session = begin(self, credential, identifier, accept_avc, **options)
        if args.recover_after and recovery_at is None:
            recovery_at = fixture.wall() + args.recover_after * 1000
        relay_events.append({'kind': 'subscribe', 't_ms': fixture.wall(), 'avc': bool(session.get('avc')),
                             'profile': session.get('avc_profile')})
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
        async with shape_downlink(gateway.server_port, port_ready, current_rate, args.rtt_ms):
            await asyncio.to_thread(stop.wait)

    proxy = threading.Thread(target=lambda: asyncio.run(forward()), daemon=True)
    try:
        direct = fixture.hub_endpoint(gateway.server_port, args.pin)
        endpoint = direct
        if 'slow' in label:
            proxy.start()
            endpoint = fixture.hub_endpoint(fixture.wait_for(lambda: port_ready and port_ready[0]), args.pin)
        probe = fixture.probe_route(PinnedHub, endpoint, 3)
        units = {}
        for profile in (('reference', 'small-idr') if args.adaptive else (args.profile,)):
            with (root / (profile + '.pkl')).open('rb') as file:
                units[profile] = pickle.load(file)
        result = fixture.trial(root / (label + '-view'), root / 'wk', source / 'linko_desktop.py',
                               (direct, endpoint), args.seconds, units, label, args.hold_local_media_ms, args.profile, args.adaptive)
        result.update(relay_events=relay_events, route_probe=probe,
                      load_average_before=load_before, load_average_after=os.getloadavg())
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
    parser.add_argument('--seconds', type=int, choices=range(8, 31), default=18)
    parser.add_argument('--profiles', nargs='+', choices=fixture.PROFILES)
    parser.add_argument('--profile', choices=fixture.PROFILES, default='reference', help=argparse.SUPPRESS)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--rates', nargs='+', type=float, help='Run each downlink rate using the same fixtures')
    parser.add_argument('--adaptive', action='store_true', help='Exercise negotiated runtime profiles')
    parser.add_argument('--candidate-revision', default='HEAD', help='Archived Git revision for the adaptive trial')
    parser.add_argument('--recover-after', type=float, default=0, help='Seconds after first media subscription')
    parser.add_argument('--recover-mbps', type=float, default=12)
    parser.add_argument('--mbps', type=float, default=1.6)
    parser.add_argument('--rtt-ms', type=int, default=600)
    parser.add_argument('--hold-local-media-ms', type=int, default=0)
    parser.add_argument('--modes', nargs='+', choices=('direct', 'slow'), default=['direct', 'slow'])
    parser.add_argument('--output', type=Path, default=Path('downlink-results.json'))
    parser.add_argument('--worker', nargs=3, help=argparse.SUPPRESS)
    parser.add_argument('--pin', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.adaptive and args.profiles:
        parser.error('--adaptive chooses its own fixtures')
    if args.worker:
        root, source, label = args.worker
        run_trial(Path(root), Path(source), label, args)
        return
    with tempfile.TemporaryDirectory(prefix='lanpilot-downlink-') as tmp:
        root = Path(tmp)
        baseline = root / 'baseline'
        candidate = root / 'candidate'
        if args.adaptive:
            candidate.mkdir()
            archive = subprocess.check_output(['git', 'archive', args.candidate_revision], cwd=_SOURCE)
            subprocess.run(['tar', '-xf', '-', '-C', str(candidate)], input=archive, check=True)
        else:
            baseline.mkdir()
            revision = '1ebc331' if args.profiles else '918911e'
            archive = subprocess.check_output(['git', 'archive', revision], cwd=_SOURCE)
            subprocess.run(['tar', '-xf', '-', '-C', str(baseline)], input=archive, check=True)
        fixture.check(fixture.quiet_run(['swiftc', str(Path(__file__).with_name('WKDownlink.swift')), '-o', str(root / 'wk')]),
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
        profiles = ['reference', 'small-idr'] if args.adaptive else args.profiles or ['reference']
        metadata = {}
        for profile in profiles:
            units, metadata[profile] = fixture.preencode(profile)
            with (root / (profile + '.pkl')).open('wb') as file:
                pickle.dump(units, file)
        sources = ({'adaptive': candidate} if args.adaptive else
                   {profile: baseline for profile in profiles} if args.profiles else
                   {'baseline': baseline, 'bounded': _SOURCE})
        files = ['lanpilot/live.py', 'lanpilot/live_view.py', 'lanpilot/live_agent.py', 'lanpilot/peer_client.py',
                 'lanpilot/peer_gateway.py', 'lanpilot/peer_hub.py', 'ui/app.js']
        report = {'started_utc': datetime.now(timezone.utc).isoformat(), 'platform': platform.platform(),
                  'pyav': fixture.av.__version__, 'seconds': args.seconds, 'downlink_mbps': args.rates or [args.mbps], 'rtt_ms': args.rtt_ms,
                  'adaptive': args.adaptive,
                  'candidate_revision': (subprocess.check_output(['git', 'rev-parse', args.candidate_revision],
                                                                 cwd=_SOURCE, text=True).strip()
                                         if args.adaptive else None),
                  'recover_after_subscribe_s': args.recover_after,
                  'recover_mbps': args.recover_mbps, 'ocr_pristine': fixture.pristine_text(root / 'wk'),
                  'hold_local_media_ms': args.hold_local_media_ms, 'fixture': metadata,
                  'harness_sha256': {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                                     for name in ('downlink_tls.py', 'downlink_fixture.py', 'WKDownlink.swift')},
                  'sha256': {variant: {file: hashlib.sha256((path / file).read_bytes()).hexdigest()
                                       for file in files} for variant, path in sources.items()}, 'trials': []}
        cases = [(repeat, rate) for repeat in range(args.repeats)
                 for rate in (args.rates or [args.mbps])[::1 if repeat % 2 == 0 else -1]]
        for repeat, rate in cases:
            for mode in args.modes[::1 if repeat % 2 == 0 else -1]:
                for variant in list(sources)[::1 if repeat % 2 == 0 else -1]:
                    label = f'{mode}-{rate:g}mbps-{repeat+1}-{variant}'
                    (root / label).mkdir()
                    profile = variant if args.profiles else 'reference'
                    adaptive = ['--adaptive', '--recover-after', str(args.recover_after),
                                '--recover-mbps', str(args.recover_mbps)] if args.adaptive else []
                    result = subprocess.run(
                        [sys.executable, __file__, '--worker',
                         str(root), str(sources[variant]), label, '--pin', pin, '--seconds', str(args.seconds),
                         '--profile', profile, '--mbps', str(rate), '--rtt-ms', str(args.rtt_ms),
                         '--hold-local-media-ms', str(args.hold_local_media_ms), *adaptive],
                        capture_output=True, text=True, timeout=args.seconds + 60,
                        env=dict(os.environ, PYTHONPATH=str(sources[variant])))
                    if result.returncode != 0:
                        raise RuntimeError(f'{label}: {result.stderr}')
                    trial = json.loads(result.stdout)
                    trial['summary']['profile'] = profile
                    report['trials'].append(trial)
                    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n')
                    print(json.dumps(trial['summary'], ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
