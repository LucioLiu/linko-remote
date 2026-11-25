"""Compare original AVC and a static image enhancement over a shaped pinned TLS stream."""
import argparse
import asyncio
import base64
from collections import Counter
from functools import lru_cache
import gzip
import hashlib
from io import BytesIO
import json
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import threading
import traceback

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
BASE = '379d180a5248ac1e80fccea0148d214d2bdae494'
GEOMETRY = '0,0,1280,720'
DURATION = 25
TILE_WIDTH, TILE_HEIGHT = 384, 216
BYTE_BUDGET = 96 * 1024
MIN_STILL = 6
TTL = 2500


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise RuntimeError(f'Expected a single source hook: {old}')
    return text.replace(old, new, 1)


def compile_viewer(source, work):
    # Reuse the product-viewer harness and its Vision scoring executable.
    swift = (source / 'tests/video_stream/WKDownlink.swift').read_text()
    swift = replace_once(swift, 'const firstDraw = new WeakSet();', r'''
          const [dx,dy]=[\(CommandLine.arguments.count > 8 ? CommandLine.arguments[8] : "0"),
                         \(CommandLine.arguments.count > 9 ? CommandLine.arguments[9] : "0")];
          \(officeOverlayJS)
          const overlay = makeOfficeOverlay(post, sample);
          const originalAvc = decodeLiveAvc;
          decodeLiveAvc = function(session, frame, capturedAt) {
            overlay.received(session, frame, capturedAt);
            return originalAvc.apply(this, arguments);
          };
          const originalClose = stopLive;
          stopLive = function() {
            overlay.reset('close'); return originalClose.apply(this, arguments);
          };
          const firstDraw = new WeakSet();''')
    swift = replace_once(swift, "post('viewer_fallback',", "overlay.reset('fallback');\n            post('viewer_fallback',")
    swift = replace_once(swift, r'''            if (\(office)) {
              const r''', r'''            if (\(office)) {
              overlay.baseDraw(session, frame, capturedAt);
              sample(frame);
              return result;
            }
            return ordinarySample(now, frame, result);
          });
          function sample(frame) {
              const r''')
    swift = replace_once(swift, 'drawImage(liveVideo, 0, 58, 384, 144,', 'drawImage(liveVideo, dx, 58+dy, 384, 144,')
    swift = replace_once(swift, 'geometry:frame.geometry, point, canvas:', 'geometry:frame.geometry, tiles:overlay.count(), point, canvas:')
    swift = replace_once(swift, '''              return result;
            }
            if (now-pixelAt''', '''          }
          function ordinarySample(now, frame, result) {
            if (now-pixelAt''')
    swift = replace_once(swift, '''            return result;
          });
          const originalFetch''', '''            return result;
          }
          const originalFetch''')
    swift = replace_once(swift, 'let previousAck=0, pending=0,', 'const staleProbes = new Set(); let previousAck=0, pending=0,')
    swift = replace_once(swift, r'if (!\(office) && !pending', '''const tilesPresent = overlay.count() > 0;
            if (!staleProbes.has(tilesPresent) && live?.frame && !freshLiveInputFrame(live)) {
              staleProbes.add(tilesPresent);
              const seq=live.seq, frameAt=live.frameAt;
              queueLive({type:'move',x:65535,y:1});
              post('stale_input_probe', {blocked:live.queue.length===0 && live.seq===seq && live.frameAt===frameAt,
                tiles:overlay.count()});
            }
            if (!pending''')
    # Inject source as a Swift raw string, so it cannot interfere with the harness string above.
    swift = 'let officeOverlayJS = #"""\n' + (HERE / 'overlay.js').read_text() + '"""#\n' + swift
    harness = work / 'WKOverlay.swift'
    harness.write_text(swift)
    binary = work / 'wk'
    subprocess.run(['swiftc', harness, '-o', binary], check=True)
    return binary


def make_office(size, seed, mode, office, fixture, Image, ImageFilter, np):
    rng = random.Random(seed)
    dx, dy = (rng.randrange(24, 690), rng.randrange(12, 132)) if seed else (0, 0)

    class ShiftedTable(office.Table):
        @lru_cache(maxsize=24)
        def picture(self, offset):
            base = super().picture(offset)
            if not (dx or dy):
                return base
            image = Image.new('RGB', base.size, '#334559')
            image.paste(base, (dx, dy))
            return image

    class Experiment(office.Office):
        def __init__(self):
            super().__init__(size, 'baseline')
            self.table = ShiftedTable()
            self.viewer += [str(dx), str(dy)]
            self.captures = {}
            self.epochs = {}
            self.last_digest = None
            self.still = 0
            self.epoch = 0

        def encode(self, index, key_requested):
            result = super().encode(index, key_requested)
            tag = result[2]
            if mode != 'avc':
                pixels = self.table.picture(tag['offset'])
                digest = hashlib.blake2s(pixels.tobytes(), digest_size=16).digest()
                if self.last_digest == digest:
                    self.still += 1
                else:
                    self.last_digest = digest
                    self.still = 0
                    self.epoch = index + 1
                self.captures[index + 1] = (self.epoch, self.still, pixels)
                self.captures.pop(index + 1 - 60, None)
            return result

        def for_frame(self, frame):
            capture = self.captures.get(frame['seq'])
            if not capture:
                return None
            epoch, still, pixels = capture
            payload = {'epoch':epoch, 'tiles':[]}
            if still < MIN_STILL:
                return payload
            if epoch not in self.epochs:
                self.epochs[epoch] = list(rank_tiles(pixels, ImageFilter, np, mode))
                if len(self.epochs) > 4:
                    del self.epochs[next(iter(self.epochs))]
            todo = self.epochs[epoch]
            patch = (todo[0] if mode == 'jpeg-diverse' else todo.pop(0)) if todo else None
            if patch:
                payload['tiles'].append(dict(patch, seq=frame['seq'], geometry=frame['geometry'],
                                             width=fixture.WIDTH, height=fixture.HEIGHT))
            return payload

        def mark_written(self, epoch):
            if mode == 'jpeg-diverse':
                self.epochs[epoch].pop(0)

    return Experiment(), (dx, dy)


def rank_tiles(pixels, ImageFilter, np, mode):
    """Use only edge energy in generic screen-space tiles after the source is still."""
    gray = pixels.convert('L')
    # High-pass energy ignores large solid fills and favors small, high-contrast detail.
    signal = (np.asarray(gray, dtype=np.int16) -
              np.asarray(gray.filter(ImageFilter.BoxBlur(2)), dtype=np.int16))
    ranked = []
    for y in range(0, pixels.height, TILE_HEIGHT):
        for x in range(0, pixels.width, TILE_WIDTH):
            w, h = min(TILE_WIDTH, pixels.width - x), min(TILE_HEIGHT, pixels.height - y)
            region = signal[y:y+h, x:x+w]
            detail = float(np.abs(region).sum()) / (TILE_WIDTH * TILE_HEIGHT)
            priority = detail
            if detail and mode == 'jpeg-diverse':
                # Downweight patterns repeated along the vertical axis. The
                # search range accommodates a range of UI text and row sizes.
                signed = region.astype(np.float32)
                power = max(1, np.mean(signed * signed))
                correlation = max((float(np.mean(signed[lag:] * signed[:-lag]) / power)
                                   for lag in range(12, min(48, h))), default=0)
                priority *= (1 - min(1, max(0, correlation))) ** 2
            ranked.append((-priority, y, x, w, h))
    used = 0
    quality = 62 if mode == 'jpeg-diverse' else 75
    for neg_priority, y, x, w, h in sorted(ranked):
        if neg_priority == 0:
            break
        data = BytesIO()
        pixels.crop((x, y, x+w, y+h)).save(data, 'JPEG', quality=quality, optimize=True)
        jpeg = data.getvalue()
        encoded = base64.b64encode(jpeg).decode('ascii')
        if used + len(encoded) > BYTE_BUDGET:
            continue
        used += len(encoded)
        yield {'x':x, 'y':y, 'w':w, 'h':h, 'priority':-neg_priority, 'jpeg':encoded}


async def trial(source, work, binary, rate, size, seed, mode, pin, office, fixture, shape):
    from lanpilot.live import LiveSessions
    from lanpilot.peer_client import PinnedHub, RelayIdentityError
    from lanpilot.peer_gateway import Handler, PeerGateway
    from PIL import Image, ImageFilter
    import numpy as np

    tag = f'{rate:g}-{seed}-{mode}'
    experiment, shift = make_office(size, seed, mode, office, fixture, Image, ImageFilter, np)
    gateway = PeerGateway(work / (tag+'-relay'), work / 'relay.crt', work / 'relay.key', 0)
    relay = threading.Thread(target=gateway.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
    relay.start()
    ready, writes = [], []
    chunk, update = Handler.stream_chunk, LiveSessions.stream_update

    def add_overlay(self, *args, **kwargs):
        value = update(self, *args, **kwargs)
        if mode != 'avc' and value and value['frame'] and 'avc_base64' in value['frame']:
            frame = value['frame']
            patch = experiment.for_frame(frame)
            if patch:
                value['frame'] = dict(frame, officeOverlay=patch)
        return value

    def count_chunk(handler, raw):
        chunk(handler, raw)
        frame = json.loads(raw).get('frame') if len(raw) > 1 else None
        if frame:
            overlay = frame.get('officeOverlay', {})
            patches = overlay.get('tiles', [])
            if patches:
                experiment.mark_written(overlay['epoch'])
            writes.append({'seq':frame['seq'], 'bytes':len(raw), 'written_ms':fixture.wall(),
                           'tiles':[{k:v for k,v in p.items() if k != 'jpeg'} |
                                    {'payload_bytes':len(p['jpeg'])} for p in patches]})

    Handler.stream_chunk, LiveSessions.stream_update = count_chunk, add_overlay
    try:
        async with shape(gateway.server_port, ready, lambda:rate, 600):
            endpoint = fixture.hub_endpoint(ready[0], pin)
            direct = fixture.hub_endpoint(gateway.server_port, pin)
            def probe():
                invalid = PinnedHub(dict(direct, pin='00' * 32), 'invalid')
                try:
                    invalid._connect()
                except RelayIdentityError:
                    return fixture.probe_route(PinnedHub, endpoint, 3)
                raise AssertionError('Incorrect certificate fingerprint was accepted')
            route = await asyncio.to_thread(probe)
            result = await asyncio.to_thread(fixture.trial, work / tag, binary, source / 'linko_desktop.py',
                                             (direct, endpoint), DURATION, {}, tag, 0, 'small-idr',
                                             True, office.GOP, experiment)
        return score(result, writes, binary, shift, experiment.table, office, fixture) | {
            'route':route, 'seed':seed, 'shift':shift, 'rate':rate, 'mode':mode}
    finally:
        Handler.stream_chunk, LiveSessions.stream_update = chunk, update
        gateway.shutdown()
        relay.join()
        gateway.server_close()
        gateway.hub.close()


def score(result, writes, binary, shift, table, office, fixture):
    frames = {d['seq']:d for d in result['frames']}
    events = result['viewer_events']
    end = next(e['t_ms'] for e in events if e['kind'] == 'window_end')
    start = next(e['t_ms'] for e in events if e['kind'] == 'start')
    dx, dy = shift
    regions = tuple(c + (dx if i % 2 == 0 else dy) for i,c in enumerate(office.ROI))
    samples = [e for e in events if e['kind'] == 'office_draw' and e['t_ms'] < end]
    if not samples:
        raise RuntimeError('No office pixels sampled')
    controls = [office.png_b64(table.picture(offset).crop(regions)) for offset in (0, 240)]
    pngs = [e.pop('png') for e in samples]
    unique = list(dict.fromkeys(controls + pngs))
    recognized = dict(zip(unique, office.ocr(binary, unique), strict=True))
    source_scores = [office.compare_fields(recognized[img], offset)
                     for img, offset in zip(controls, (0,240), strict=True)]
    for event, png in zip(samples, pngs, strict=True):
        frame = frames[event['seq']]
        event.update({k:frame[k] for k in ('phase','offset','captured_ms')},
                     draw_ms=event['t_ms'], **office.compare_fields(recognized[png], frame['offset']))
        if event['geometry'] != GEOMETRY or event['canvas'] != [fixture.WIDTH,fixture.HEIGHT]:
            raise AssertionError('Unexpected rendered geometry')

    begins = {phase:min(d['captured_ms'] for d in frames.values() if d['phase'] == phase)
              for phase in ('startup','scroll','stopped','resume')}
    # Consider every visible transition, including clears and redraws. During the
    # stationary source phase a wrong phase or fewer than 18 matches breaks the run.
    summaries = {}
    for phase, stop in [('startup','scroll'), ('stopped','resume')]:
        first = start if phase == 'startup' else begins[phase]
        last = begins[stop]
        timeline = [e for e in samples if first <= e['t_ms'] < last]
        exact = [e['phase'] == phase and e['exact'] == e['total'] == 18 for e in timeline]
        stable = None
        for i,e in enumerate(timeline):
            if not exact[i] or i and exact[i-1]:
                continue
            ended = next((later['t_ms'] for later, ok in zip(timeline[i+1:], exact[i+1:]) if not ok), last)
            if ended - e['t_ms'] >= 5000:
                stable = e['t_ms']
                break
        summaries[phase] = {'first_18_ms': next((round(e['t_ms']-first) for e, ok in zip(timeline,exact) if ok), None),
                            'first_sustained_18_ms':round(stable-first) if stable else None,
                            'confirmed_after_ms':round(stable-first+5000) if stable else None,
                            'from_first_draw_ms':round(stable-timeline[0]['t_ms']) if stable else None,
                            'best':max((e['exact'] for e in timeline if e['phase'] == phase), default=0),
                            'duration_ms':round(last-first), 'samples':len(timeline)}

    resume = begins['resume']
    resumed = next((e for e in samples if e['phase'] == 'resume'), None)
    clears = [e for e in events if e['kind'] == 'overlay_clear']
    applied = [e for e in events if e['kind'] == 'overlay_apply']
    result['overlay'] = {
        'source_exact':[row['exact'] for row in source_scores], **summaries,
        'media_bytes':sum(w['bytes'] for w in writes),
        'overlay_payload_bytes':sum(t['payload_bytes'] for w in writes for t in w['tiles']),
        'tile_records_sent':sum(len(w['tiles']) for w in writes),
        'tile_records_applied':len(applied),
        'tile_records_rejected':dict(Counter(e['reason'] for e in events if e['kind'] == 'overlay_reject')),
        'restores':dict(Counter(e['reason'] for e in clears)),
        'all_restores_pixel_identical':all(e['identical'] for e in clears),
        'all_apply_input_unchanged':all(e['inputUnchanged'] for e in applied),
        'stale_input_probes':[e for e in events if e['kind'] == 'stale_input_probe'],
        'guard_checks':[e for e in events if e['kind'] == 'overlay_guard'],
        'first_resume_draw_ms':round(resumed['t_ms']-resume) if resumed else None,
        'resume_overlays_on_moving_frame':sum(bool(e['tiles']) for e in samples if e['phase'] == 'resume'),
        'stopped_patches_applied_after_resume':sum(e['t_ms']>=resume and frames[e['seq']]['phase']=='stopped' for e in applied),
        'last_old_patch_clear_after_resume_ms':next((round(e['t_ms']-resume) for e in clears
                                                    if e['t_ms']>=resume and frames[e['seq']]['phase']=='stopped'),None),
        'visible_old_overlay_at_resume':next((e['tiles'] for e in reversed(samples)
                                              if e['t_ms'] < resume), 0),
    }
    # The legacy fixture's success flag depends on its motion pixel sampler,
    # which is bypassed by the office sampler.
    result['summary'].pop('successful', None)
    result['overlay']['first_draw_ms'] = round(samples[0]['t_ms'] - start)
    result['media_writes'] = writes
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--rates', type=float, nargs='+', default=[1,1.6])
    parser.add_argument('--seeds', type=int, nargs='+', default=[0,17,93])
    parser.add_argument('--modes', nargs='+', choices=['avc','jpeg','jpeg-diverse'], default=['avc','jpeg'])
    parser.add_argument('--size', default='1220x860')
    args = parser.parse_args()
    data = {'base':BASE, 'settings':vars(args) | {'output':str(args.output), 'rtt_ms':600,
            'tile':[TILE_WIDTH,TILE_HEIGHT], 'still_intervals':MIN_STILL, 'tile_budget_base64':BYTE_BUDGET,
            'display_ttl_ms':TTL},
            'prototype_sha256':{name:hashlib.sha256((HERE/name).read_bytes()).hexdigest()
                                for name in ('run.py','overlay.js')}, 'runs':{}, 'failures':{}}

    def save():
        pending = args.output.with_suffix('.pending')
        with gzip.open(pending, 'wt', encoding='utf-8') as file:
            json.dump(data, file, ensure_ascii=False)
        pending.replace(args.output)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='lanpilot-office-overlay-') as tmp:
        work = Path(tmp)
        source = work / 'exact-base'
        source.mkdir()
        archive = subprocess.check_output(['git','archive',BASE], cwd=ROOT)
        subprocess.run(['tar','-xf','-','-C',source], input=archive, check=True)
        sys.path[:0] = [str(source / 'tests/video_stream'), str(source)]
        import office_readability as office
        import downlink_fixture as fixture
        from downlink_tls import shape_downlink

        binary = compile_viewer(source, work)
        (work / 'loopback.sb').write_text('(version 1)\n(allow default)\n(deny network*)\n'
            '(allow network-bind (local ip "localhost:*"))\n'
            '(allow network-inbound (local ip "localhost:*"))\n'
            '(allow network-outbound (remote ip "localhost:*"))\n')
        cert, key = work / 'relay.crt', work / 'relay.key'
        subprocess.run(['openssl','req','-x509','-newkey','ec','-pkeyopt','ec_paramgen_curve:prime256v1',
                        '-nodes','-subj','/CN=Loopback overlay','-days','1','-keyout',key,'-out',cert],
                       check=True, capture_output=True)
        pin = hashlib.sha256(subprocess.check_output(['openssl','x509','-in',cert,'-outform','DER'])).hexdigest()
        size = tuple(map(int,args.size.split('x')))
        for rate_index, rate in enumerate(args.rates):
            for index, seed in enumerate(args.seeds):
                modes = args.modes if (index + rate_index) % 2 == 0 else args.modes[::-1]
                for mode in modes:
                    label = f'{rate:g}-{seed}-{mode}'
                    print(f'Running {label}', flush=True)
                    try:
                        result = asyncio.run(trial(source, work, binary, rate, size, seed, mode, pin,
                                                   office, fixture, shape_downlink))
                        data['runs'][label] = result
                        print(label, result['overlay'], flush=True)
                    except Exception:
                        data['failures'][label] = traceback.format_exc()
                        print(data['failures'][label], flush=True)
                    save()
    return int(bool(data['failures']))


if __name__ == '__main__':
    sys.exit(main())
