"""Measure exact office fields on the product viewer through loopback TLS."""
import argparse
import asyncio
from functools import lru_cache, partial
import hashlib
from http.server import ThreadingHTTPServer
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unicodedata

from PIL import Image, ImageDraw, ImageFont

from avc_quality import Handler, image_b64, pixel_error, png_b64, wire_frame
from downlink_fixture import FPS, HEIGHT, WIDTH, av, encoder_for, hub_endpoint, ms_data, trial
from downlink_tls import shape_downlink

ROOT = Path(__file__).resolve().parents[2]
REVISION = '63e9617'

ROI = (0, 58, 384, 202)
START_SCROLL = 160
STOP_SCROLL = 200
RESUME_SCROLL = 360
STEP = 24
GOP = 3 * FPS
FONT = '/System/Library/Fonts/Hiragino Sans GB.ttc'
COLS = (16, 128, 257)


def fields(row):
    names = ('周晓岚', '林嘉宁', '陈雨桐', '许嘉悦', '吴明轩', '徐思远')
    return (f'{12_508 + row * 173:,}.{(43 + row * 13) % 100:02}',
            f'2026-11-{(row * 7) % 28 + 1:02}', names[row % len(names)])


class Table:
    def __init__(self):
        self.font = ImageFont.truetype(FONT, 16)
        self.header = Image.new('RGB', (WIDTH, 58), '#20344c')
        draw = ImageDraw.Draw(self.header)
        draw.text((16, 8), '邻控合成测试 · 月度明细', font=self.font, fill='white')
        for x, title in zip(COLS, ('金额', '日期', '姓名'), strict=True):
            draw.text((x, 33), title, font=self.font, fill='white')
        self.rows = Image.new('RGB', (WIDTH, STEP * 90))
        draw = ImageDraw.Draw(self.rows)
        for row in range(90):
            y = row * STEP
            draw.rectangle((0, y, WIDTH, y + STEP - 2),
                           fill='#f5f8fc' if row % 2 else 'white')
            draw.line((0, y + STEP - 1, WIDTH, y + STEP - 1), fill='#dbdfeb')
            columns = (*COLS, *range(396, 1220, 144))
            values = (*fields(row), *(f'{row+1:03} · {name}' for name in
                          ('待复核明细', '项目办公费用', '当月合成记录', '流水核验', '合成数据', '办公样例')))
            for x, value in zip(columns, values, strict=True):
                draw.text((x, y + 3), value, font=self.font, fill='#23354c')

    @lru_cache(maxsize=24)
    def picture(self, offset):
        frame = Image.new('RGB', (WIDTH, HEIGHT))
        frame.paste(self.header)
        frame.paste(self.rows.crop((0, offset, WIDTH, offset + HEIGHT - 58)), (0, 58))
        return frame

    @lru_cache(maxsize=24)
    def video_frame(self, offset):
        return av.VideoFrame.from_image(self.picture(offset)).reformat(format='yuv420p')


class Office:
    def __init__(self, size, variant):
        self.table = Table()
        self.viewer = ['office', *map(str, size)]
        self.variant = variant
        self.encoder = None
        self.gop_offset = 0
        self.last_key = 0
        self.buffer = 100
        self.offset = None
        self.stable_for = 0
        self.attempted_boost = False
        self.first_draw = threading.Event()

    def encode(self, index, key_requested):
        offset = (min(max(index - START_SCROLL + 1, 0), STOP_SCROLL - START_SCROLL)
                  + max(index - RESUME_SCROLL + 1, 0)) * 6
        phase = ('startup' if index < START_SCROLL else 'scroll' if index < STOP_SCROLL
                 else 'stopped' if index < RESUME_SCROLL else 'resume')
        if offset == self.offset:
            self.stable_for += 1
        else:
            self.stable_for = 0
            self.attempted_boost = False
        self.offset = offset
        can_boost = self.variant == 'boost' and (phase != 'startup' or self.first_draw.is_set())
        boost = can_boost and self.stable_for >= 6 and not self.attempted_boost
        buffer = 275 if boost or (self.stable_for and self.buffer == 275) else 100
        if key_requested or boost or buffer != self.buffer or index - self.last_key >= GOP:
            self.encoder = encoder_for(WIDTH, HEIGHT, 750, buffer, gop=GOP)
            self.gop_offset = 0
            self.last_key = index
            self.buffer = buffer
        picture = self.table.video_frame(offset)
        picture.pts = self.gop_offset
        packet, = self.encoder.encode(picture)
        if packet.is_keyframe != (self.gop_offset == 0):
            raise RuntimeError('Unexpected office GOP')
        unit = bytes(packet)
        if boost:
            self.attempted_boost = True
        current = self.gop_offset
        self.gop_offset += 1
        return unit, current, {'offset': offset, 'phase': phase, 'buffer_kbit': buffer, 'boost': boost}


def normalize(text):
    return ''.join(c for c in unicodedata.normalize('NFKC', text) if not c.isspace())


def compare_fields(observations, offset):
    expected = {}
    seen = {}
    first_row = (offset + STEP - 1) // STEP
    for row in range(first_row, (offset + ROI[3] - ROI[1]) // STEP):
        for column, value in enumerate(fields(row)):
            expected[row, column] = value
    for observation in observations:
        x, y, _, h = observation['box']
        x = x * (ROI[2] - ROI[0])
        y = (y + h / 2) * (ROI[3] - ROI[1]) + offset
        column = min(range(3), key=lambda i: abs(x - COLS[i]))
        label = int(y // STEP), column
        if label in expected:
            seen.setdefault(label, []).append((x, normalize(observation['text'])))
    scored = [{'row': row, 'column': column, 'expected': value,
               'observed': ''.join(text for _, text in sorted(seen.get((row, column), [])))}
              for (row, column), value in expected.items()]
    return {'fields': scored,
            'exact': sum(cell['expected'] == cell['observed'] for cell in scored),
            'total': len(scored)}


def ocr(binary, pngs):
    output = subprocess.check_output([binary, '--ocr-boxes', '--office-ocr'],
                                     input=json.dumps(pngs).encode(), timeout=150)
    return json.loads(output)


def diagnostic(binary, directory, size, source):
    directory.mkdir(exist_ok=True)
    table = Table()
    contents = []
    pictures = {}
    for n, offset in enumerate((0, 240)):
        for buffer in (100, 275):
            encoder = encoder_for(WIDTH, HEIGHT, 750, buffer, gop=GOP)
            for frame_no in range(7):
                picture = table.video_frame(offset)
                picture.pts = frame_no
                packet, = encoder.encode(picture)
                contents.append(wire_frame(frame_no + 1, bytes(packet), frame_no == 0,
                                           WIDTH, HEIGHT, frame_no in (0, 6)))
                if frame_no == 0:
                    decoder = av.CodecContext.create('h264', 'r')
                    decoder.thread_count = 1
                frame, = decoder.decode(packet)
                if frame_no in (0, 6):
                    key = f'{n}-{buffer}-{frame_no}'
                    pictures[key] = (offset, packet.size, frame.to_image())
    with ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(source / 'ui'))) as server:
        server.frames = json.dumps(contents).encode()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            output = subprocess.check_output(
                [binary, f'http://127.0.0.1:{server.server_port}/', '--quality', *size.split('x')], timeout=85)
        finally:
            server.shutdown()
            thread.join()
    result = {}
    samples = []
    for (key, (offset, length, decoded)), line in zip(pictures.items(), output.splitlines(), strict=True):
        info = json.loads(line)
        source = table.picture(offset).crop(ROI)
        images = {'source': source, 'software': decoded.crop(ROI), 'canvas': image_b64(info['png']).crop(ROI)}
        for label, scale in [('snapshot_1x', 1), ('snapshot_dpr', info['dpr'])]:
            x, y, w, h = info['rect']
            bounds = ((x + ROI[0]*w/WIDTH)*scale, (y + ROI[1]*h/HEIGHT)*scale,
                      (x + ROI[2]*w/WIDTH)*scale, (y + ROI[3]*h/HEIGHT)*scale)
            size = (round(bounds[2] - bounds[0]), round(bounds[3] - bounds[1]))
            images[label.replace('snapshot', 'source')] = source.resize(size, Image.Resampling.BILINEAR)
            images[label] = image_b64(info[label]).transform(
                size, Image.Transform.EXTENT, bounds, Image.Resampling.BILINEAR)
        result[key] = {'offset': offset, 'bytes': length, 'layout': {
            name: info[name] for name in ('viewport', 'dpr', 'rect', 'fit')}, 'stages': {}}
        for label, image in images.items():
            image.save(directory / f'{key}-{label}.png')
            samples.append((key, label, image, source))
    for (key, label, image, source), observations in zip(
            samples, ocr(binary, [png_b64(image) for _, _, image, _ in samples]), strict=True):
        result[key]['stages'][label] = {
            **compare_fields(observations, result[key]['offset']), 'size': image.size,
            'vs_source': pixel_error(image.resize(source.size, Image.Resampling.BILINEAR), source)}
    return result


def jpeg_control(binary, directory):
    table = Table()
    report = []
    pngs = []
    for offset in (0, 240):
        source = table.picture(offset)
        for scope, picture in (('ROI', source.crop(ROI)), ('desktop', source)):
            stream = BytesIO()
            picture.save(stream, 'JPEG', quality=75, optimize=True)
            jpeg = stream.getvalue()
            decoded = Image.open(BytesIO(jpeg))
            roi = decoded if scope == 'ROI' else decoded.crop(ROI)
            roi.save(directory / f'jpeg-q75-{offset}-{scope}.png')
            pngs.append(png_b64(roi))
            base64_bytes = (len(jpeg) + 2) // 3 * 4
            report.append({'offset': offset, 'scope': scope, 'bytes': len(jpeg),
                           'base64_bytes': base64_bytes, 'size': picture.size,
                           'serialization_ms': {rate: round(base64_bytes * 8 / rate / 1000)
                                                for rate in (1, 1.6)}})
    for row, observations in zip(report, ocr(binary, pngs), strict=True):
        row.update(compare_fields(observations, row['offset']))
    return report


def phase_summary(draws, phase, began, ended):
    relevant = [draw for draw in draws if draw['phase'] == phase and draw['draw_ms'] < ended]
    first = relevant[0] if relevant else None
    last_bad = max((i for i, row in enumerate(relevant) if row['exact'] != row['total']), default=-1)
    stable = relevant[last_bad + 1] if last_bad + 1 < len(relevant) else None
    accepted = stable is not None and ended - stable['draw_ms'] >= 5000
    return {'first_ms': round(first['draw_ms'] - began) if first else None,
            'first_exact': [first['exact'], first['total']] if first else None,
            'first_readable_ms': next((round(d['draw_ms'] - began) for d in relevant
                                      if d['exact'] == d['total']), None),
            'stable_readable_ms': round(stable['draw_ms'] - began) if accepted else None,
            'stable_after_first_ms': round(stable['draw_ms'] - first['draw_ms']) if accepted else None,
            'last_bad_ms': round(relevant[last_bad]['draw_ms'] - began) if last_bad >= 0 else None,
            'observed_until_ms': round(ended - began), 'draws': len(relevant),
            'exact_draws': sum(row['exact'] == row['total'] for row in relevant),
            'best_exact': max((row['exact'] for row in relevant), default=0)}


def score_trial(binary, result, began, table):
    frames = {frame['seq']: frame for frame in result['frames']}
    events = result['viewer_events']
    draw_times = {event['seq']: event['t_ms'] for event in events if event['kind'] == 'draw'}
    ended = next(event['t_ms'] for event in events if event['kind'] == 'window_end')
    draws = [event for event in events if event['kind'] == 'office_draw' and event['t_ms'] < ended]
    if not draws:
        raise RuntimeError('No office frames drawn')
    layouts = {tuple(draw['viewport']) for draw in draws}
    assert len(layouts) == 1
    for draw, observations in zip(draws, ocr(binary, [draw['png'] for draw in draws]), strict=True):
        image = image_b64(draw.pop('png'))
        frame = frames[draw['seq']]
        source = table.picture(frame['offset']).crop(ROI)
        draw.update({key: frame[key] for key in ('offset', 'phase', 'bytes', 'buffer_kbit', 'boost', 'captured_ms')},
                    **compare_fields(observations, frame['offset']), draw_ms=draw_times[draw['seq']],
                    vs_source=pixel_error(image.resize(source.size, Image.Resampling.BILINEAR), source))
        assert draw['point'] == {'x': 20316, 'y': 11796} and draw['geometry'] == '0,0,1280,720'
        assert draw['canvas'] == [WIDTH, HEIGHT] and draw['idr'] == frame['key']
    captured = {i: min(frame['captured_ms'] for frame in frames.values() if frame['phase'] == i)
                for i in ('startup', 'scroll', 'stopped', 'resume')}
    result['office'] = {
        'layout': {name: draws[0][name] for name in ('viewport', 'dpr', 'rect', 'roiRaster', 'canvas', 'fit')},
        'startup': phase_summary(draws, 'startup', captured['startup'], captured['scroll']),
        'stopped': phase_summary(draws, 'stopped', captured['stopped'], captured['resume']),
        'resume': phase_summary(draws, 'resume', captured['resume'], ended),
        'first_draw_key_bytes': draws[0]['bytes'],
        'first_draw_buffer_kbit': draws[0]['buffer_kbit'],
        'connection_to_first_ms': round(draws[0]['draw_ms'] - began),
        'draw_age_ms': ms_data(draw['draw_ms'] - draw['captured_ms'] for draw in draws),
        'resume_draw_age_ms': ms_data(draw['draw_ms'] - draw['captured_ms'] for draw in draws if draw['phase'] == 'resume'),
        'resume_gap_ms': ms_data(right['draw_ms'] - left['draw_ms']
                                 for left, right in zip(draws, draws[1:]) if right['phase'] == 'resume'),
        'resume_first_idr': next((draw['idr'] for draw in draws if draw['phase'] == 'resume'), None),
        'boosts_captured': [frame['bytes'] for frame in frames.values() if frame['boost']],
        'boosts_drawn': [draw['seq'] for draw in draws if draw['boost']],
        'idr_bytes': ms_data(draw['bytes'] for draw in draws if draw['idr']),
        'predicted_bytes': ms_data(draw['bytes'] for draw in draws if not draw['idr']),
    }
    return result


async def live_trial(root, source, binary, pin, rate, size, variant, label):
    from lanpilot.peer_gateway import Handler as RelayHandler, PeerGateway
    gateway = PeerGateway(root / (label + '-relay'), root / 'relay.crt', root / 'relay.key', 0)
    thread = threading.Thread(target=gateway.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
    thread.start()
    ready, writes = [], []
    office = Office(size, variant)
    chunk = RelayHandler.stream_chunk

    def count_chunk(handler, raw):
        if len(raw) > 1:
            value = json.loads(raw)
            if value.get('frame'):
                writes.append({'seq': value['frame']['seq'], 'bytes': len(raw)})
        return chunk(handler, raw)

    RelayHandler.stream_chunk = count_chunk
    try:
        async with shape_downlink(gateway.server_port, ready, lambda: rate, 600):
            endpoint = hub_endpoint(ready[0], pin)
            direct = hub_endpoint(gateway.server_port, pin)
            result = await asyncio.to_thread(trial, root / label, binary, source / 'linko_desktop.py',
                                             (direct, endpoint), 27, {}, label, 0, 'small-idr',
                                             True, GOP, office)
        started = next(event['t_ms'] for event in result['viewer_events'] if event['kind'] == 'start')
        result = score_trial(binary, result, started, office.table)
        if result['office']['first_draw_buffer_kbit'] != 100:
            raise RuntimeError('The first visible frame did not use the low AVC budget')
        return result | {'downlink_writes': writes}
    finally:
        RelayHandler.stream_chunk = chunk
        gateway.shutdown()
        thread.join()
        gateway.server_close()
        gateway.hub.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--rates', nargs='+', type=float, default=[1, 1.6])
    parser.add_argument('--sizes', nargs='+', default=['1220x860', '900x700'])
    parser.add_argument('--variants', nargs='+', choices=('baseline', 'boost'), default=['baseline', 'boost'])
    parser.add_argument('--reverse', action='store_true')
    parser.add_argument('--diagnostic-only', action='store_true')
    parser.add_argument('--jpeg-only', action='store_true')
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='lanpilot-office-') as directory:
        root = Path(directory)
        binary = root / 'wk'
        subprocess.run(['swiftc', str(Path(__file__).with_name('WKDownlink.swift')), '-o', binary], check=True)
        if args.jpeg_only:
            (out / 'jpeg-control.json').write_text(json.dumps(jpeg_control(binary, out), ensure_ascii=False, indent=2) + '\n')
            return
        revision = subprocess.check_output(['git', 'rev-parse', REVISION], cwd=ROOT, text=True).strip()
        source = root / 'baseline'
        source.mkdir()
        archive = subprocess.check_output(['git', 'archive', revision], cwd=ROOT)
        subprocess.run(['tar', '-xf', '-', '-C', source], input=archive, check=True)
        sys.path.insert(0, str(source))
        evidence = {'baseline': revision,
                    'diagnostic': {size: diagnostic(binary, out / size, size, source)
                                   for size in dict.fromkeys(args.sizes)},
                    'jpeg_control': jpeg_control(binary, out),
                    'runs': {}}

        def save():
            (out / 'results.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n')

        save()
        if args.diagnostic_only:
            return
        (root / 'loopback.sb').write_text('(version 1)\n(allow default)\n(deny network*)\n'
                                         '(allow network-bind (local ip "localhost:*"))\n'
                                         '(allow network-inbound (local ip "localhost:*"))\n'
                                         '(allow network-outbound (remote ip "localhost:*"))\n')
        cert, key = root / 'relay.crt', root / 'relay.key'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1',
                        '-nodes', '-subj', '/CN=Loopback office', '-days', '1', '-keyout', key, '-out', cert],
                       check=True, capture_output=True)
        pin = hashlib.sha256(subprocess.check_output(['openssl', 'x509', '-in', cert, '-outform', 'DER'])).hexdigest()
        pairs = [(size, rate, variant) for size in args.sizes for rate in args.rates for variant in args.variants]
        for size, rate, variant in reversed(pairs) if args.reverse else pairs:
            label = f'{size}-{rate:g}-{variant}'
            result = asyncio.run(live_trial(root, source, binary, pin, rate, tuple(map(int, size.split('x'))), variant, label))
            evidence['runs'][label] = result
            save()
            print(label, json.dumps(result['office'], ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
