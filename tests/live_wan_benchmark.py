"""Sample a paired peer through the checkout's real viewer. No input or image files.

Run from the project root with PYTHONPATH=. python3 tests/live_wan_benchmark.py
PEER_ID. Close the interactive viewer first. The relay arbitrates active sessions.
"""
import argparse
import hashlib
import json
import math
import statistics
import threading
import time

from lanpilot.live_view import LiveView
from lanpilot.peer_client import PinnedHub, default_home


class TimedViewerHub(PinnedHub):
    def __init__(self, endpoint, credential):
        super().__init__(endpoint, credential)
        self.calls = []
        self.repeats = []
        self.delta_sizes = []
        self.delta_requests = []
        self.last_digest = None
        self.duplicate_payloads = 0

    def call(self, operation, body=None):
        started = time.monotonic()
        result = super().call(operation, body)
        frame = result.get('frame')
        payload = frame.get('jpeg_base64') if frame else None
        elapsed = time.monotonic() - started
        if payload:
            digest = hashlib.sha256(payload.encode('ascii')).digest()
            self.duplicate_payloads += digest == self.last_digest
            self.last_digest = digest
        if frame and 'delta_base64' in frame:
            self.delta_sizes.append(len(frame['delta_base64']))
            self.delta_requests.append(elapsed)
            return result
        if frame and 'repeat_after' in frame:
            self.repeats.append(elapsed)
            return result
        self.calls.append((elapsed,
                           len(payload) if payload else 0,
                           result.get('waited_ms')))
        return result


def ms_summary(seconds):
    values = sorted(seconds)
    if values:
        return {'samples': len(values), 'p50': round(1000 * statistics.median(values)),
                'p95': round(1000 * values[math.ceil(.95 * len(values)) - 1])}
    return {'samples': 0}


class MeasuredView(LiveView):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.last_digest = None
        self.content_changes = 0
        self.wire_frames = 0
        self.encoded_bytes = 0

    def publish(self, value, roundtrip=0):
        wire = value.get('frame')
        super().publish(value, roundtrip)
        if wire and not value.get('error'):
            self.wire_frames += 1
            self.encoded_bytes += len(wire.get('jpeg_base64', '')) + len(wire.get('delta_base64', ''))
            if 'repeat_after' not in wire:
                image = self.value['frame']['jpeg_base64']
                digest = hashlib.sha256(image.encode('ascii')).digest()
                self.content_changes += self.last_digest != digest
                self.last_digest = digest


def measure(peer, seconds, stream=False):
    config = json.loads((default_home() / 'client.json').read_text())
    endpoint, credential = config['endpoint'], config['credential']
    control = PinnedHub(endpoint, credential)
    media = TimedViewerHub(endpoint, credential)
    start = time.monotonic()
    session = control.call('live-open', {'peer': peer})
    identifier = session['id']
    opened = time.monotonic()
    view = MeasuredView(media, identifier, threading.Event(),
                        stream_version=session.get('view_stream') if stream else None)
    previous = None
    arrivals = []
    error = ''
    try:
        view.start()
        while time.monotonic() - opened < seconds:
            state = view.snapshot()
            frame = state.get('frame')
            if frame and frame['seq'] != previous:
                arrivals.append(time.monotonic())
                previous = frame['seq']
            error = state.get('error', '')
            if error:
                break
            time.sleep(.035)
        end = time.monotonic()
    finally:
        view.close()
        try:
            control.call('live-close', {'id': identifier})
        except PermissionError as exc:
            if str(exc) != '连续桌面会话已结束':
                raise
        if view.thread:
            view.thread.join(timeout=2)
    calls = list(media.calls)
    sizes = [size for _, size, _ in calls if size]
    elapsed = end - opened
    return {'seconds': round(elapsed, 2), 'completed_window': elapsed >= seconds and not error,
            'distinct_updates_per_second': round(view.content_changes / elapsed, 2),
            'open_ms': round((opened-start)*1000),
            'first_frame_ms': round((arrivals[0]-start)*1000) if arrivals else None,
            'visible_frames': len(arrivals), 'error': error,
            'stream_requested': stream, 'stream_advertised': session.get('view_stream') == 1,
            'wire_frames': view.wire_frames, 'wire_image_bytes': view.encoded_bytes,
            'distinct_content_updates': view.content_changes,
            'frame_interval_ms': ms_summary(b-a for a,b in zip(arrivals, arrivals[1:])),
            'frame_request_ms': ms_summary(duration for duration,size,_ in calls if size),
            'empty_request_ms': ms_summary(duration for duration,size,_ in calls if not size),
            'repeat_request_ms': ms_summary(media.repeats),
            'delta_request_ms': ms_summary(media.delta_requests),
            'delta_payload_bytes': sum(media.delta_sizes),
            'full_payload_bytes': sum(sizes),
            'duplicate_full_payloads': media.duplicate_payloads,
            'median_base64_bytes': statistics.median(sizes) if sizes else None,
            'relay_reported_wait': any(wait is not None for _,_,wait in calls)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('peer')
    parser.add_argument('--seconds', type=int, choices=range(5, 301), default=30, metavar='5..300')
    parser.add_argument('--stream', action='store_true', help='Negotiate bounded HTTPS streaming')
    options = parser.parse_args()
    print(json.dumps(measure(options.peer, options.seconds, options.stream), indent=2, ensure_ascii=False))
