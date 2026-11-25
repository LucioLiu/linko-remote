"""Measure pointer input to decoded target pixels on an explicitly selected Windows peer.

Uses the crosshair drawn into captures by Desktop.cs. This moves the remote
pointer between two central points, without clicking or typing. Pick an idle,
static desktop. Results end at decoded pixels in memory, before presentation.
Requires Pillow (also used by tests/video_stream).
"""
import argparse
import base64
import io
import json
import threading
import time

from PIL import Image, ImageChops, ImageStat

from lanpilot.live_view import LiveView
from lanpilot.peer_client import PinnedHub, default_home


def patches(frame, target):
    raw = base64.b64decode(frame['jpeg_base64'], validate=True)
    with Image.open(io.BytesIO(raw)) as image:
        x, y = (round(v * (n - 1) / 65535) for v, n in zip(target, image.size))
        def patch(offset):
            return image.crop((x + offset - 7, y - 7, x + offset + 8, y + 8)).convert('L')
        return patch(0), patch(32)


def fresh_frame(view, max_age):
    with view.changed:
        state = view.snapshot()
        if state['error']:
            raise RuntimeError(state['error'])
        if state['frame'] and state['frame_age_ms'] <= max_age:
            return view.value['frame']


def measure(peer, count):
    config = json.loads((default_home() / 'client.json').read_text())
    def hub():
        return PinnedHub(config['endpoint'], config['credential'])
    control = hub()
    identifier = control.call('live-open', {'peer': peer})['id']
    view = LiveView(hub(), identifier, threading.Event())
    samples = []
    try:
        view.start()
        for seq in range(1, count + 1):
            target = (31000 + (seq % 2) * 2500, 31000)
            limit = time.monotonic() + 15
            while time.monotonic() < limit:
                frame = fresh_frame(view, 1100)
                if frame:
                    break
                time.sleep(.02)
            else:
                raise RuntimeError('未收到适合操作的新画面')
            before = patches(frame, target)
            sample = {'seq': seq, 'pixel_ms': None, 'ack_ms': None}
            samples.append(sample)
            start = time.monotonic()
            control.call('live-input', {'id': identifier, 'seq': seq, 'geometry': frame['geometry'],
                                        'events': [{'type': 'move', 'x': target[0], 'y': target[1]}]})
            seen = frame['seq']
            while time.monotonic() - start < 8:
                current = fresh_frame(view, 1500)
                if current and current['seq'] != seen and sample['pixel_ms'] is None:
                    seen = current['seq']
                    after = patches(current, target)
                    delta, adjacent = [ImageStat.Stat(ImageChops.difference(a,b)).mean[0]
                                       for a,b in zip(before, after)]
                    if delta >= 5 and adjacent < 2:
                        sample['pixel_ms'] = round(1000 * (time.monotonic()-start))
                if view.snapshot()['ack'] >= seq and sample['ack_ms'] is None:
                    sample['ack_ms'] = round(1000 * (time.monotonic()-start))
                if sample['pixel_ms'] is not None and sample['ack_ms'] is not None:
                    break
                time.sleep(.02)
            else:
                raise RuntimeError('未确认目标像素变化或输入回执；停止测量')
        return {'samples': samples, 'error': ''}
    except (RuntimeError, OSError, ValueError, PermissionError) as exc:
        return {'samples': samples, 'error': str(exc)}
    finally:
        view.close()
        try:
            control.call('live-close', {'id': identifier})
        except PermissionError as exc:
            if str(exc) != '连续桌面会话已结束':
                raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('peer')
    parser.add_argument('--count', type=int, choices=range(1, 21), default=5)
    args = parser.parse_args()
    print(json.dumps(measure(args.peer, args.count), indent=2, ensure_ascii=False))
