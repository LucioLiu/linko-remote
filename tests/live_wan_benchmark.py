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
        if frame and "repeat_after" in frame:
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


def measure(peer, seconds):
    config = json.loads((default_home() / 'client.json').read_text())
    endpoint, credential = config['endpoint'], config['credential']
    control = PinnedHub(endpoint, credential)
    media = TimedViewerHub(endpoint, credential)
    start = time.monotonic()
    identifier = control.call('live-open', {'peer': peer})['id']
    opened = time.monotonic()
    view = LiveView(media, identifier, threading.Event())
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
    return {'seconds': round(end-opened, 2), 'open_ms': round((opened-start)*1000),
            'first_frame_ms': round((arrivals[0]-start)*1000) if arrivals else None,
            'visible_frames': len(arrivals), 'error': error,
            'frame_interval_ms': ms_summary(b-a for a,b in zip(arrivals, arrivals[1:])),
            'frame_request_ms': ms_summary(duration for duration,size,_ in calls if size),
            'empty_request_ms': ms_summary(duration for duration,size,_ in calls if not size),
            'repeat_request_ms': ms_summary(media.repeats),
            'full_payload_bytes': sum(sizes),
            'duplicate_full_payloads': media.duplicate_payloads,
            'median_base64_bytes': statistics.median(sizes) if sizes else None,
            'relay_reported_wait': any(wait is not None for _,_,wait in calls)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('peer')
    parser.add_argument('--seconds', type=int, choices=range(5, 301), default=30, metavar='5..300')
    options = parser.parse_args()
    print(json.dumps(measure(options.peer, options.seconds), indent=2, ensure_ascii=False))
