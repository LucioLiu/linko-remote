"""Loopback-only synthetic Windows capture and offscreen product viewer fixture."""
import base64
from collections import Counter, defaultdict
from contextlib import ExitStack
from fractions import Fraction
from functools import lru_cache
import hashlib
import json
import math
import secrets
import re
import statistics
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import av
from PIL import Image
from benchmark import ScrollingText

WIDTH, HEIGHT, FPS, GOP, SCENE_FRAMES = 1280, 720, 20, 20, 60
AVC_SAMPLE_ROI = (8, 52, 432, 156)

PROFILES = {
    'reference': (1280, 720, 3500, 400),
    'bitrate': (1280, 720, 750, 400),
    'small-idr': (1280, 720, 750, 100),
    '540p': (960, 540, 750, 100),
    '360p': (640, 360, 750, 100),
}
EXPERIMENTAL_PROFILES = {'vbv175': (1280, 720, 750, 175)}
ALL_PROFILES = PROFILES | EXPERIMENTAL_PROFILES


def wall():
    return time.time_ns() / 1e6

def ms_data(values):
    values = sorted(values)
    if not values:
        return {'n': 0}
    return {'n': len(values), 'p50': round(statistics.median(values), 3),
            'p95': round(values[math.ceil(.95 * len(values)) - 1], 3),
            'min': round(values[0], 3), 'max': round(values[-1], 3)}

def wait_for(check, seconds=8):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if value := check():
            return value
        time.sleep(.02)
    raise RuntimeError('Timed out waiting for test readiness')

def terminate(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

def quiet_run(command, **kwargs):
    return subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, timeout=15, **kwargs)

def check(result, description):
    if result.returncode:
        raise RuntimeError(f'{description}: exit {result.returncode}')
    return result.stdout.strip()

def encoder_for(width, height, kbps, vbv, gop=GOP):
    encoder = av.CodecContext.create('libx264', 'w')
    encoder.width, encoder.height = width, height
    encoder.time_base, encoder.framerate = Fraction(1, FPS), FPS
    encoder.pix_fmt, encoder.gop_size, encoder.max_b_frames, encoder.thread_count = 'yuv420p', gop, 0, 1
    encoder.options = {'profile': 'baseline', 'preset': 'veryfast', 'tune': 'zerolatency',
                       'x264-params': f'annexb=1:repeat-headers=1:scenecut=0:open-gop=0:'
                                      f'vbv-maxrate={kbps}:vbv-bufsize={vbv}:vbv-init=0.65'}
    encoder.bit_rate = kbps * 1000
    return encoder


def preencode(profile, gop=GOP):
    width, height, kbps, vbv = ALL_PROFILES[profile]
    scene = ScrollingText(WIDTH, HEIGHT, 'full')
    pictures = []
    for i in range(SCENE_FRAMES):
        source = Image.fromarray(scene.frame(i))
        if (width, height) != (WIDTH, HEIGHT):
            source = source.resize((width, height), Image.Resampling.BILINEAR)
        pictures.append(av.VideoFrame.from_image(source).reformat(format='yuv420p'))
    fixture, digest = {}, hashlib.sha256()
    for start in range(SCENE_FRAMES):
        encoder = encoder_for(width, height, kbps, vbv, gop)
        decoder = av.CodecContext.create('h264', 'r')
        decoder.thread_count = 1
        last = None
        for offset in range(min(gop, SCENE_FRAMES - start)):
            picture = pictures[start + offset]
            picture.pts = offset
            packet, = encoder.encode(picture)
            if packet.pts != offset or packet.dts != offset or packet.is_keyframe != (offset == 0):
                raise RuntimeError('Unexpected fixture GOP or order')
            decoded, = decoder.decode(packet)
            pixels = hashlib.sha256(decoded.to_ndarray(format='gray')).digest()
            if (decoded.width, decoded.height) != (width, height) or pixels == last:
                raise RuntimeError('Fixture frames did not change')
            last = pixels
            unit = bytes(packet)
            digest.update(unit)
            fixture[start, offset] = unit
        if encoder.encode(None) or decoder.decode(None):
            raise RuntimeError('Fixture codec delayed output')
    return fixture, {'width': width, 'height': height, 'target_fps': FPS,
                     'max_gop': gop, 'scene_cycle_frames': SCENE_FRAMES,
                     'verified_access_units': len(fixture), 'sha256_ordered_access_units': digest.hexdigest(),
                     'idr_payload_bytes': ms_data(len(v) for (_, offset), v in fixture.items() if offset == 0),
                     'p_payload_bytes': ms_data(len(v) for (_, offset), v in fixture.items() if offset != 0),
                     'profile': profile,
                     'target_kbps': kbps, 'vbv_kbit': vbv, 'encoder': 'libx264 baseline, ABR/VBV, zerolatency, no B frames'}

def hub_endpoint(port, pin):
    return {'url': f'https://127.0.0.1:{port}', 'pin': pin}

def trial(root, binary, engine, endpoints, duration, fixture, label, hold_local_media_ms, profile, adaptive=False, gop=GOP):
    width, height, _, _ = ALL_PROFILES[profile]
    from lanpilot import live_agent
    from lanpilot.live import AVC_HEADER
    from lanpilot.peer_client import PinnedHub, atomic_json
    root.mkdir()
    sender_endpoint, viewer_endpoint = endpoints
    sender_token, viewer_token = (secrets.token_urlsafe(32) for _ in range(2))
    events, captures, uploads, receipts, mailbox_errors = [], {}, {}, {}, []
    active_threads = []
    stop = threading.Event()
    cpu_clock_ref = wall() - time.monotonic()*1000
    sender_events = []
    class SenderHub(PinnedHub):
        def _connect(self):
            started = wall()
            try:
                return super()._connect()
            finally:
                sender_events.append({'kind': 'sender_tls', 'started_ms': started, 'ended_ms': wall()})
        def call(self, operation, body=None):
            if not adaptive and operation in ('live-agent-state', 'live-exchange'):
                body = {key: value for key, value in body.items() if key != 'accept_avc_profile'}
            started = wall()
            value = None
            if operation == 'live-avc-batch':
                records = body['frames']
            elif operation == 'live-frame' and 'avc_base64' in body['frame']:
                records = [body['frame']]
            else:
                records = []
            try:
                value = super().call(operation, body)
                return value
            finally:
                ended = wall()
                sender_events.append({'kind': operation, 'started_ms': started, 'ended_ms': ended,
                                      'value': {key: value[key] for key in
                                                ('live_avc', 'frame_seq', 'active', 'need_idr', 'sessions', 'avc_profile')
                                                if key in value} if value else None})
                if records:
                    batch_last = AVC_HEADER.unpack(base64.b64decode(records[-1]['avc_base64'][:64]))[2]
                    for record in records:
                        header = base64.b64decode(record['avc_base64'][:64])
                        _, size, seq, _, _, _, key, *_ = AVC_HEADER.unpack(header)
                        uploads[seq] = {'seq':seq, 'started_ms':started, 'ended_ms':ended, 'key':bool(key),
                                        'bytes':size, 'batch_last':batch_last,
                                        'confirmed':value is not None and value.get('frame_seq') == seq,
                                        'relay_avc': None if value is None else value.get('live_avc')}
    worker = SimpleNamespace(config={'endpoint':sender_endpoint, 'credential':sender_token},
                             agent=SimpleNamespace(workspace=root), incoming=True, stop_event=stop)
    agent = live_agent.LiveAgent(worker, SenderHub, atomic_json)
    agent.avc_enabled = True
    live_agent.windows_tick_ms = lambda: time.monotonic_ns() // 1_000_000
    home = root / 'viewer'
    home.mkdir()
    (home / 'client.json').write_text(json.dumps({'endpoint':viewer_endpoint, 'credential':viewer_token,
        'name':'WAN fixture viewer', 'ssh':[], 'auto_update_check':False}))
    (root / 'endpoint.json').write_text(json.dumps(viewer_endpoint))
    reg_sender = PinnedHub(sender_endpoint, sender_token)
    reg_viewer = PinnedHub(viewer_endpoint, viewer_token)

    def mailbox():
        seq, receipt, index_start, seen_key, identity = 0, 0, 0, None, None
        current_profile = profile
        next_capture = time.monotonic()
        while not stop.is_set():
            control = agent.read('live-control.json')
            if control.get('expires', 0) <= time.time() or not control.get('live_avc'):
                stop.wait(.007)
                continue
            session = control['id']
            if identity is None:
                identity = session
                next_capture = time.monotonic()
            if session != identity:
                return
            for path in sorted(root.glob(f'{session}-*.live-input.json')):
                batch = json.loads(path.read_text())
                number = batch['seq']
                if number == receipt + 1 and batch['expires'] > time.time():
                    if batch['geometry'] != '0,0,1280,720' or batch['events'] != [{'type':'move','x':number,'y':1}]:
                        mailbox_errors.append(batch)
                        return
                    receipt = number
                    receipts[number] = wall()
                    atomic_json(root / 'live-receipt.json', {'id':session, 'seq':receipt})
            now = time.monotonic()
            if now < next_capture:
                stop.wait(min(.007, next_capture - now))
                continue
            next_capture = max(now - .01, next_capture) + 1/FPS
            tick, captured = live_agent.windows_tick_ms(), wall()
            index = seq % SCENE_FRAMES
            key = control['avc_key']
            next_profile = ('small-idr' if control.get('avc_profile') == 2 else 'reference') if adaptive else profile
            if key != seen_key or index == 0 or index - index_start >= gop or next_profile != current_profile:
                index_start, seen_key = index, key
            offset = index - index_start
            current_profile = next_profile
            unit = fixture[current_profile][index_start, offset]
            seq += 1
            record = AVC_HEADER.pack(b'LPA1', len(unit), seq, tick, width, height,
                                     int(offset == 0), 0, 0, WIDTH, HEIGHT) + unit
            captures[seq] = {'seq':seq, 'captured_ms':captured, 'key':offset == 0, 'bytes':len(unit), 'profile':current_profile,
                             'scene_index':index, 'gop_offset':offset}
            path = root / (f'live-avc-{seq % 8}.json' if control.get('avc_batch') else 'live-frame.json')
            atomic_json(path, {'id':session, 'seq':seq, 'capture_tick':tick,
                        'idr':offset == 0, 'avc_base64':base64.b64encode(record).decode('ascii')})
    def collect(proc):
        for line in proc.stdout:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    def close_hub(hub):
        if hub.connection is not None:
            hub.connection.close()
    with ExitStack() as stack:
        for hub in (reg_sender, reg_viewer, agent.hub, agent.frame_hub):
            stack.callback(close_hub, hub)
        target = reg_sender.call('register', {'name':'WAN fixture sender','platform':'windows',
                                               'live_version':1,'live_avc':1})['id']
        reg_sender.call('availability', {'enabled': True})
        packaged = subprocess.Popen(['sandbox-exec', '-f', str(root.parent/'loopback.sb'), sys.executable, str(engine),
                                     '--home',str(home),'--endpoint',str(root/'endpoint.json')],
                                    cwd=engine.parent, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        stack.callback(terminate, packaged)
        marker = home / 'shell-session.json'
        wait_for(marker.is_file)
        browser_url = json.loads(marker.read_text())['url']
        reg_viewer.call('register', {'name':'WAN fixture viewer','platform':'macos'})
        code = reg_sender.call('code')['code']
        approval = reg_viewer.call('connect', {'code':code})['approval']
        reg_sender.call('approve', {'approval':approval,'allow':True})
        for func in (agent.run, agent.frames, mailbox):
            thread = threading.Thread(target=func, daemon=True)
            thread.start()
            active_threads.append(thread)
        def stop_threads():
            stop.set()
            for thread in active_threads:
                thread.join(4)
            if any(thread.is_alive() for thread in active_threads):
                raise RuntimeError('Test sender threads failed to stop')
        stack.callback(stop_threads)
        view = subprocess.Popen([str(binary), browser_url, target, str(duration), str(hold_local_media_ms)], text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        stack.callback(view.stdout.close)
        stack.callback(terminate, view)
        reader = threading.Thread(target=collect, args=(view,), daemon=True)
        reader.start()
        try:
            wait_for(lambda: any(e['kind'] in {'done','close_error','javascript_error'} for e in list(events))
                             or view.poll() is not None, duration+8)
        except RuntimeError:
            pass
        if not any(e['kind'] == 'done' for e in events):
            raise RuntimeError(f'Offscreen viewer never completed: {dict(Counter(e["kind"] for e in events))}')
    reader.join(2)
    result = summarize(label, cpu_clock_ref, events, captures, uploads, receipts, width, height)
    if mailbox_errors:
        raise RuntimeError(f'Input geometry or sequencing failed: {mailbox_errors}')
    result['sender_events'] = sender_events
    samples = [e for e in events if e['kind'] == 'text_sample']
    for sample in samples:
        capture = captures[sample['seq']]
        if sample['idr'] != capture['key']:
            raise RuntimeError('Canvas sample frame type differs from the capture')
        sample.update(scene_index=capture['scene_index'], gop_offset=capture['gop_offset'],
                      profile=capture['profile'])
    measure_text(binary, samples)
    result['summary']['text_phrases_exact'] = sum(row['exact_phrases'] for row in samples)
    result['summary']['text_phrases_total'] = sum(len(row['expected']) for row in samples)
    result['summary']['text_by_frame_type'] = {}
    for key in (True, False):
        rows = [row for row in samples if row['idr'] == key]
        chars = sum(row['reference_characters'] for row in rows)
        result['summary']['text_by_frame_type']['idr' if key else 'p'] = {
            'samples': len(rows), 'exact': sum(row['exact_phrases'] for row in rows),
            'phrases': sum(len(row['expected']) for row in rows),
            'cer': sum(row['character_edits'] for row in rows) / chars if chars else None,
        }
    return result


def normalize(text):
    return re.sub(r'[^A-Z0-9]', '', text.upper())


def edit_distance(source, target):
    previous = list(range(len(target) + 1))
    for i, left in enumerate(source, 1):
        row = [i]
        for j, right in enumerate(target, 1):
            row.append(min(row[-1] + 1, previous[j] + 1, previous[j - 1] + (left != right)))
        previous = row
    return previous[-1]


@lru_cache(maxsize=SCENE_FRAMES)
def text_slots(scene_index):
    # Use the synthetic font's actual glyph bounds, including only complete
    # lines. ScrollingText uses y+4 and x=12+212*column.
    origin_x, origin_y, right, bottom = AVC_SAMPLE_ROI
    scroll = scene_index * 7
    font = ScrollingText.font_for_text()
    slots = []
    for row in range((origin_y + scroll) // 26, (bottom + scroll) // 26 + 1):
        number = row % 72
        for col in (0, 1):
            text = f'{number:03d}   Ref {number * 17 + col:04d}   Review'
            a, b, c, d = font.getbbox(text)
            x, y = 12 + col * 212, row * 26 + 4 - scroll
            if x + a >= origin_x and x + c <= right and y + b >= origin_y and y + d <= bottom:
                slots.append((normalize(text), x - origin_x, (b + d)/2 + y - origin_y))
    return slots


def score_text(slots, observations):
    # Vision can return columns in either order. Score fixed screen slots
    # using each observation's position and count missing text as deletions.
    width = AVC_SAMPLE_ROI[2] - AVC_SAMPLE_ROI[0]
    height = AVC_SAMPLE_ROI[3] - AVC_SAMPLE_ROI[1]
    matched = defaultdict(list)
    extra = 0
    for observation in observations:
        x, y, _, h = observation['box']
        x, y = x * width, (y + h / 2) * height
        text = normalize(observation['text'])
        candidates = [(abs(y - cy), index) for index, (_, left, cy) in enumerate(slots)
                      if left - 8 <= x < left + 165 and abs(y - cy) < 12]
        if candidates:
            matched[min(candidates)[1]].append((x, text))
        else:
            # Exclude text from lines whose glyph bounds straddle the ROI edge.
            if slots and min(slot[2] for slot in slots) - 12 <= y <= max(slot[2] for slot in slots) + 12:
                extra += len(text)
    expected = [slot[0] for slot in slots]
    observed = [''.join(part for _, part in sorted(matched[i])) for i in range(len(slots))]
    chars = sum(map(len, expected))
    errors = extra + sum(edit_distance(left, right) for left, right in zip(expected, observed, strict=True))
    return {'expected': expected, 'matched': observed, 'observations': observations,
            'edits': errors, 'chars': chars, 'exact': sum(a == b for a, b in zip(expected, observed, strict=True))}


def pristine_text(binary):
    from io import BytesIO
    scene = ScrollingText(WIDTH, HEIGHT, 'full')
    samples = []
    for sequence in (1, 9, 21, 37, 53):
        image = Image.fromarray(scene.frame(sequence - 1)).crop(AVC_SAMPLE_ROI)
        png = BytesIO()
        image.save(png, format='PNG')
        samples.append({'seq': sequence, 'png': base64.b64encode(png.getvalue()).decode()})
    measure_text(binary, samples)
    return samples


def measure_text(binary, samples):
    # OCR runs after the timed window. The PNGs come from the product canvas,
    # normalized to a fixed viewport crop so downscaling does not enlarge text.
    if not samples:
        return
    images = [event.pop('png') for event in samples]
    recognized = subprocess.run([str(binary), '--ocr-boxes'], input=json.dumps(images), text=True,
                                capture_output=True, check=True, timeout=60)
    for event, observations, png in zip(samples, json.loads(recognized.stdout), images, strict=True):
        index = event.get('scene_index', (event['seq'] - 1) % SCENE_FRAMES)
        score = score_text(text_slots(index), observations)
        event.update(expected=score['expected'], recognized=[o['text'] for o in observations],
                     matched=score['matched'], exact_phrases=score['exact'], character_edits=score['edits'],
                     reference_characters=score['chars'],
                     png_sha256=hashlib.sha256(base64.b64decode(png)).hexdigest())


def summarize(label, cpu_clock_ref, events, captures, uploads, receipts, width, height):
    by_kind = {kind:[event for event in events if event['kind'] == kind]
               for kind in set(event['kind'] for event in events)}
    start = by_kind['start'][0]['t_ms']
    end_event = by_kind['window_end'][0]
    end = end_event['t_ms']
    intake = {e['seq']:e for e in by_kind.get('receive', []) if e['t_ms'] <= end}
    draw = {e['seq']:e for e in by_kind.get('draw', []) if e['codec'] == 'H.264' and e['t_ms'] <= end}
    queued, acked, accepted = ({e['seq']:e['t_ms'] for e in by_kind.get(kind, []) if e.get('ok',True)}
                              for kind in ('input_queue','input_ack','input_accepted'))
    frames = []
    for seq, capture in captures.items():
        row = dict(capture)
        if seq in uploads:
            row.update(uploads[seq])
        if seq in intake:
            row['receiver_ms'] = intake[seq]['t_ms']
            row['receiver_product_age_ms'] = intake[seq]['estimated_age_ms']
        if seq in draw:
            row['draw_ms'] = draw[seq]['t_ms']
        frames.append(row)
    drift = round(wall() - time.monotonic()*1000 - cpu_clock_ref, 3)
    pixels = by_kind.get('pixel', [])
    seconds = (end-start)/1000
    first_capture = min((row['captured_ms'] for row in frames), default=None)
    and_after = [e['t_ms'] for e in draw.values()]
    summary = {'mode':label, 'window_s':round(seconds,3), 'capture_count':sum(f['captured_ms']<=end for f in frames),
               'upload_attempts':len(uploads), 'upload_confirmed':sum(r['confirmed'] for r in uploads.values()),
               'upload_confirmed_idr':sum(r['confirmed'] and r['key'] for r in uploads.values()),
               'upload_rejected_or_unconfirmed':sum(not r['confirmed'] for r in uploads.values()),
               'receiver_frames':len(intake), 'draw_frames':len(draw),
               'effective_draw_fps':round(len(draw)/seconds,3),
               'draw_fps_excluding_startup':round((len(draw)-1)*1000/(max(and_after)-min(and_after)),3) if len(draw)>1 else None,
               'sampled_canvas_pixels':len(pixels), 'distinct_canvas_signatures':len(set(p['hash'] for p in pixels)),
               'dimensions_verified':bool(pixels) and all((p['width'],p['height']) == (width,height) for p in pixels),
               'negotiated_avc':by_kind.get('open',[{}])[0].get('avc') == 1,
               'first_open_ack_ms':round(by_kind['open'][0]['t_ms']-start,3) if by_kind.get('open') else None,
               'first_receiver_ms':round(min(e['t_ms'] for e in intake.values())-start,3) if intake else None,
               'first_draw_ms':round(min(and_after)-start,3) if draw else None,
               'first_capture_from_open_start_ms':round(first_capture-start,3) if first_capture else None,
               'upload_ack_ms':ms_data(f['ended_ms']-f['started_ms'] for f in frames if f.get('confirmed')),
               'upload_ack_idr_ms':ms_data(f['ended_ms']-f['started_ms'] for f in frames if f.get('confirmed') and f['key']),
               'upload_ack_p_ms':ms_data(f['ended_ms']-f['started_ms'] for f in frames if f.get('confirmed') and not f['key']),
               'receiver_age_ms':ms_data(f['receiver_ms']-f['captured_ms'] for f in frames if 'receiver_ms' in f),
               'draw_age_ms':ms_data(f['draw_ms']-f['captured_ms'] for f in frames if 'draw_ms' in f),
               'decode_to_canvas_ms':ms_data(f['draw_ms']-f['receiver_ms'] for f in frames if 'draw_ms' in f and 'receiver_ms' in f),
               'receiver_estimated_age_ms':ms_data(f['receiver_product_age_ms'] for f in frames if 'receiver_ms' in f),
               'draw_interval_ms':ms_data(b-a for a,b in zip(sorted(and_after),sorted(and_after)[1:])),
               'input_queued':len(by_kind.get('input_queue', [])),
               'input_dropped_before_submit':len(by_kind.get('input_dropped', [])), 'input_accepted':len(accepted), 'input_mailbox_receipts':len(receipts), 'input_acked':len(acked),
               'input_accept_ms':ms_data(accepted[k]-v for k,v in queued.items() if k in accepted),
               'input_ack_ms':ms_data(acked[k]-v for k,v in queued.items() if k in acked),
               'wall_vs_monotonic_drift_ms':drift, 'ended_active':end_event['active'],
               'ended_jpeg':end_event['jpeg'], 'ended_halt':end_event['halt']}
    summary['successful'] = (summary['negotiated_avc'] and summary['dimensions_verified'] and
                             summary['distinct_canvas_signatures'] >= 3 and summary['input_queued'] >= 3 and
                             len(queued) == len(accepted) == len(acked) == len(receipts) and
                             not end_event['jpeg'] and end_event['active'] and not end_event['halt'] and abs(drift) < 5)
    result = {'summary':summary,'frames':frames,'viewer_events':events,'synthetic_receipts_ms':receipts}
    annotate_upload_window(result)
    return result

def annotate_upload_window(result):
    end = next(event['t_ms'] for event in result['viewer_events'] if event['kind'] == 'window_end')
    attempts = [frame for frame in result['frames'] if frame.get('started_ms', math.inf) <= end]
    confirmations = [frame for frame in attempts if frame.get('confirmed') and frame['ended_ms'] <= end]
    def times(records):
        return ms_data(f['ended_ms']-f['started_ms'] for f in records)
    result['summary']['upload_within_window'] = {
        'attempts_started':len(attempts), 'confirmed':len(confirmations),
        'confirmed_idr':sum(frame['key'] for frame in confirmations),
        'unconfirmed_at_window_end':len(attempts)-len(confirmations),
        'ack_ms':times(confirmations),
        'ack_idr_ms':times(f for f in confirmations if f['key']),
        'ack_p_ms':times(f for f in confirmations if not f['key']),
    }

def probe_route(hub_type, endpoint, count):
    probe = hub_type(endpoint, secrets.token_urlsafe(32))
    start = time.monotonic()
    try:
        probe.call('register', {'name':'WAN route probe','platform':'macos'})
        opened = round((time.monotonic()-start)*1000,3)
        state_ms = []
        for _ in range(count):
            start = time.monotonic()
            probe.call('state')
            state_ms.append((time.monotonic()-start)*1000)
        return {'cold_register_ms':opened,'warm_state_ms':ms_data(state_ms),'raw_warm_state_ms':state_ms}
    finally:
        if probe.connection is not None:
            probe.connection.close()
