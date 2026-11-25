"""Compare the same desktop pixels through software AVC and the product WKWebView."""
import argparse
import base64
from collections import defaultdict
from functools import partial
import gzip
import hashlib
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import platform
from pathlib import Path
import subprocess
import tempfile
import threading

import numpy as np
from PIL import Image
from benchmark import ScrollingText
from downlink_fixture import (
    ALL_PROFILES, AVC_SAMPLE_ROI, FPS, HEIGHT, PROFILES, SCENE_FRAMES, WIDTH, av, encoder_for,
    ms_data, preencode, score_text, text_slots,
)

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = Path(__file__).with_name('avc_encoding_results.json.gz')
SAMPLES = {
    0: 'static', 4: 'static', 9: 'static', 19: 'static',
    21: 'scroll', 24: 'scroll', 30: 'scroll', 35: 'scroll', 39: 'scroll',
    40: 'recovery', 41: 'recovery', 44: 'recovery', 49: 'recovery', 59: 'recovery',
}

def png_b64(image):
    buffer = BytesIO()
    image.save(buffer, 'PNG')
    return base64.b64encode(buffer.getvalue()).decode()


def image_b64(png):
    return Image.open(BytesIO(base64.b64decode(png))).convert('RGB')


def ocr(binary, records):
    result = subprocess.run([str(binary), '--ocr-boxes'],
                            input=json.dumps([png_b64(image) for _, _, image in records]),
                            text=True, capture_output=True, check=True, timeout=110)
    for (sample, stage, image), observations in zip(records, json.loads(result.stdout), strict=True):
        sample['stages'][stage].update(score_text(text_slots(sample['scene_index']), observations))


def crop(image, extent, native=False):
    # Transform from the physical-desktop ROI to the coordinates of this
    # stage without padding or an intermediate whole-image resize.
    x, y, width, height = extent
    a, b, c, d = AVC_SAMPLE_ROI
    size = (round((c-a)*width/WIDTH), round((d-b)*height/HEIGHT)) if native else (c-a, d-b)
    return image.transform(size, Image.Transform.EXTENT,
                           (x+a*width/WIDTH, y+b*height/HEIGHT, x+c*width/WIDTH, y+d*height/HEIGHT),
                           Image.Resampling.BILINEAR)


def pixel_error(a, b):
    delta = np.asarray(a, dtype=np.float64) - np.asarray(b, dtype=np.float64)
    abs_delta = np.abs(delta)
    return {'rgb_mae': round(float(abs_delta.mean()), 3),
            'rgb_rmse': round(float(np.sqrt(np.square(delta).mean())), 3),
            'max_error': int(abs_delta.max())}


def quality_record(directory, sample, stage, image, reference):
    filename = f'{sample["id"]}-{stage}.png'
    image.save(directory / filename)
    sample['stages'][stage] = {'png': filename, 'sha256': hashlib.sha256(image.tobytes()).hexdigest(),
                               'vs_source': pixel_error(image, reference)}


def wire_frame(seq, packet, key, width, height, sample):
    import struct
    header = struct.pack('<4sIQQHHIiiII', b'LPA1', len(packet), seq, seq, width, height, int(key),
                         0, 0, WIDTH, HEIGHT)
    return {'seq': seq, 'idr': key, 'geometry': f'0,0,{WIDTH},{HEIGHT}', 'sample': sample,
            'avc_base64': base64.b64encode(header + packet).decode()}


def decode_sequence(width, height, inputs):
    decoder = None
    for packet, key in inputs:
        if key:
            decoder = av.CodecContext.create('h264', 'r')
            decoder.thread_count = 1
        frame, = decoder.decode(av.Packet(packet))
        if (frame.width, frame.height) != (width, height):
            raise RuntimeError('Decoded dimensions changed')
        yield frame.to_image()


def phase_stream(scene, config):
    width, height, kbps, vbv = config
    pictures = []
    for i in range(3*FPS):
        index = max(0, min(FPS, i-FPS))
        rgb = Image.fromarray(scene.frame(index)).resize((width, height), Image.Resampling.BILINEAR)
        image = av.VideoFrame.from_image(rgb).reformat(format='yuv420p')
        image.pts = i
        pictures.append((index, image))

    streams = []
    for gop in (2*FPS, 3*FPS):
        resets_on_stop = gop == 2*FPS
        encoder = encoder_for(width, height, kbps, vbv, gop=gop)
        sequence = []
        for i, (index, image) in enumerate(pictures):
            packet, = encoder.encode(image)
            key = i % gop == 0
            if packet.pts != i or packet.dts != i or packet.is_keyframe != key:
                raise RuntimeError('Unexpected encoder frame order or GOP')
            phase = SAMPLES.get(i)
            if phase == 'recovery':
                phase = 'recovery-idr' if resets_on_stop else 'recovery-continuous'
            elif not resets_on_stop:
                phase = None
            sequence.append((index, bytes(packet), key, phase))
        if encoder.encode(None):
            raise RuntimeError('Unexpected delayed encoder output')
        streams.append(sequence)
    return streams


def archived_streams(profile):
    archive = json.loads(gzip.decompress(ARCHIVE.read_bytes()))['runs']
    old_counts = defaultdict(lambda: [0, 0])
    wanted = defaultdict(set)
    hashes = defaultdict(set)
    for run in archive.values():
        for trial in run['trials']:
            captures = {frame['seq']: frame for frame in trial['frames']}
            actual_gops = {}
            first = 0
            for seq, frame in sorted(captures.items()):
                if frame['key']:
                    first = seq
                actual_gops[seq] = (first - 1) % SCENE_FRAMES, seq - first
                if frame.get('draw_ms') and frame['profile'] == profile:
                    old_counts['draw_idr' if frame['key'] else 'draw_p'][0] += 1
            for sample in (e for e in trial['viewer_events'] if e['kind'] == 'text_sample'):
                seq = sample['seq']
                frame = captures[seq]
                if frame['profile'] == profile:
                    start, offset = actual_gops[seq]
                    wanted[start].add(offset)
                    hashes[start, offset].add(sample['png_sha256'])
                    key = 'sample_idr' if frame['key'] else 'sample_p'
                    old_counts[key][0] += 1
                    old_counts[key][1] += sample['exact_phrases']
    if not wanted:
        return [], old_counts, hashes
    units, metadata = preencode(profile)
    for run in archive.values():
        expected = run['fixture'].get(profile)
        if expected and expected['sha256_ordered_access_units'] != metadata['sha256_ordered_access_units']:
            raise RuntimeError('Archived access unit hash differs')
    streams = [[(start + i, units[start, i], i == 0, 'historical' if i in offsets else None)
                for i in range(max(offsets) + 1)] for start, offsets in sorted(wanted.items())]
    return streams, old_counts, hashes


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/frames':
            body = self.server.frames
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            super().do_GET()

    def do_POST(self):
        self.send_error(404)

    def log_message(self, *_):
        pass


def measure(binary, server, scene, profile, config, streams, directory):
    width, height, _, _ = config
    payload, samples, records = [], [], []
    sizes = []
    for group, stream in enumerate(streams):
        frames = decode_sequence(width, height, ((packet, key) for _, packet, key, _ in stream))
        for seq, ((index, packet, key, phase), decoded) in enumerate(zip(stream, frames, strict=True), 1):
            payload.append(wire_frame(seq, packet, key, width, height, bool(phase)))
            sizes.append({'idr': key, 'bytes': len(packet)})
            if phase is None:
                continue
            source = Image.fromarray(scene.frame(index)).crop(AVC_SAMPLE_ROI)
            software = crop(decoded, (0, 0, width, height))
            sample = {'id': f'{profile}-{phase}-{group}-{seq}', 'scene_index': index,
                      'sequence': seq, 'idr': key, 'phase': phase, 'stages': {},
                      'unit_sha256': hashlib.sha256(packet).hexdigest()}
            samples.append(sample)
            for stage, image in [('source', source), ('software', software)]:
                quality_record(directory, sample, stage, image, source)
                records.append((sample, stage, image))
    server.frames = json.dumps(payload).encode()
    run = subprocess.run([str(binary), f'http://127.0.0.1:{server.server_port}/', '--quality'],
                         capture_output=True, text=True, timeout=100)
    if run.returncode:
        raise RuntimeError(f'WK quality playback failed: {run.stderr}')
    for sample, line in zip(samples, run.stdout.splitlines(), strict=True):
        info = json.loads(line)
        if (info.pop('seq'), info.pop('width'), info.pop('height')) != (sample['sequence'], width, height):
            raise RuntimeError('Canvas sample lost frame alignment')
        sample['legacy_canvas_png_sha256'] = hashlib.sha256(base64.b64decode(info.pop('textPng'))).hexdigest()
        source = Image.open(directory / sample['stages']['source']['png']).convert('RGB')
        software = Image.open(directory / sample['stages']['software']['png']).convert('RGB')
        canvas = crop(image_b64(info.pop('png')), (0, 0, width, height))
        quality_record(directory, sample, 'canvas', canvas, source)
        sample['stages']['canvas']['vs_software'] = pixel_error(canvas, software)
        records.append((sample, 'canvas', canvas))
        for stage in ('snapshot_1x', 'snapshot_dpr'):
            viewport = image_b64(info.pop(stage))
            vx, vy = info['viewport']
            rx, ry, rw, rh = info['rect']
            ratio = info['dpr'] if stage == 'snapshot_dpr' else 1
            if viewport.size != (round(vx*ratio), round(vy*ratio)):
                raise RuntimeError(f'Unexpected {stage} raster size {viewport.size} for {info}')
            sx, sy = viewport.width / vx, viewport.height / vy
            extent = rx*sx, ry*sy, rw*sx, rh*sy
            image = crop(viewport, extent)
            roi_raster = crop(viewport, extent, native=True)
            quality_record(directory, sample, stage, image, source)
            sample['stages'][stage].update(vs_canvas=pixel_error(image, canvas), raster_size=list(viewport.size),
                                          ocr_raster_size=list(roi_raster.size))
            roi_raster.save(directory / f'{sample["id"]}-{stage}-native.png')
            records.append((sample, stage, roi_raster))
        sample['layout'] = info
    ocr(binary, records)
    return {'samples': samples, 'bitrate': {'all': sum(s['bytes'] for s in sizes),
             'idr': ms_data(s['bytes'] for s in sizes if s['idr']),
             'p': ms_data(s['bytes'] for s in sizes if not s['idr'])}}


def summarize(samples):
    groups = defaultdict(list)
    for sample in samples:
        for stage, value in sample['stages'].items():
            for phase in (sample['phase'], f'{sample["phase"]}/frame-{"idr" if sample["idr"] else "p"}'):
                groups[phase, stage].append(value)
    result = defaultdict(dict)
    for (phase, stage), values in groups.items():
        chars = sum(v['chars'] for v in values)
        result[phase][stage] = {'frames': len(values), 'phrases': sum(len(v['expected']) for v in values),
                               'exact': sum(v['exact'] for v in values), 'chars': chars,
                               'cer': round(sum(v['edits'] for v in values) / chars, 4),
                               'source_mae': round(float(np.mean([v['vs_source']['rgb_mae'] for v in values])), 3)}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--profiles', nargs='+', choices=ALL_PROFILES, default=list(ALL_PROFILES))
    parser.add_argument('--skip-archive', action='store_true')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    scene = ScrollingText(WIDTH, HEIGHT, 'full')
    results = {}
    metadata = {
        'platform': platform.platform(), 'pyav': av.__version__, 'roi': AVC_SAMPLE_ROI,
        'ocr': {'engine': 'macOS Vision', 'language': 'en-US', 'language_correction': False,
                'automatic_language_detection': False},
        'sha256': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in (
            'ui/app.js', 'ui/style.css', 'ui/index.html', 'tests/video_stream/benchmark.py',
            'tests/video_stream/avc_quality.py', 'tests/video_stream/downlink_fixture.py',
            'tests/video_stream/WKDownlink.swift')},
    }
    with tempfile.TemporaryDirectory(prefix='lanpilot-quality-') as tmp:
        binary = Path(tmp) / 'wk'
        subprocess.run(['swiftc', str(Path(__file__).with_name('WKDownlink.swift')), '-o', str(binary)], check=True)
        with ThreadingHTTPServer(('127.0.0.1', 0), partial(Handler, directory=str(ROOT / 'ui'))) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                for profile in args.profiles:
                    config = ALL_PROFILES[profile]
                    streams = phase_stream(scene, config)
                    result = {'config': config}
                    result['phases'] = measure(binary, server, scene, profile, config, streams, args.output)
                    if not args.skip_archive and profile in PROFILES:
                        streams, counts, hashes = archived_streams(profile)
                        result['archived_counts'] = counts
                        result['archived'] = measure(binary, server, scene, profile, config, streams, args.output)
                        for sample in result['archived']['samples']:
                            offset = sample['sequence'] - 1
                            start = sample['scene_index'] - offset
                            sample['archived_canvas_png_hashes'] = sorted(hashes[start, offset])
                            sample['archived_canvas_png_matched'] = (
                                sample['legacy_canvas_png_sha256'] in sample['archived_canvas_png_hashes'])
                    for section in ('phases', 'archived'):
                        if section in result:
                            result[section]['summary'] = summarize(result[section]['samples'])
                    results[profile] = result
                    (args.output / 'results.json').write_text(json.dumps({'environment': metadata, 'profiles': results}, indent=2) + '\n')
                    print(f"{profile}: {len(result['phases']['samples'])} phase samples", flush=True)
            finally:
                server.shutdown()
                thread.join()


if __name__ == '__main__':
    main()
