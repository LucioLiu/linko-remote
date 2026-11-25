import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import random
import secrets
import ssl
import struct
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
import zlib

from lanpilot.live_delta import (
    BLOCK_LENGTH, BLOCK_SIZE, HEADER, MAX_JPEG_BASE64, MAX_JPEG_BYTES,
    DeltaError, decode_delta, encode_delta,
)
from lanpilot.live_view import LiveView
from lanpilot.peer_client import PinnedHub
from lanpilot.peer_gateway import PeerGateway


# A 2x2 JPEG. A large JPEG comment segment makes the byte-level fixture
# exercise multiple dictionaries without an image library or a screen capture.
JPEG = base64.b64decode(
    '/9j/4AAQSkZJRgABAQAASABIAAD/4QBMRXhpZgAATU0AKgAAAAgAAYdpAAQAAAABAAAAGgAAAAAAA6ABAAMAAAABAAEAAKACAAQAAAABAAAAAqADAAQAAAABAAAAAgAAAAD/'
    '7QA4UGhvdG9zaG9wIDMuMAA4QklNBAQAAAAAAAA4QklNBCUAAAAAABDUHYzZjwCyBOmACZjs+EJ+/8AAEQgAAgACAwEiAAIRAQMRAf/EAB8AAAEFAQEBAQEBAAAAAAAAAAAB'
    'AgMEBQYHCAkKC//EALUQAAIBAwMCBAMFBQQEAAABfQECAwAEEQUSITFBBhNRYQcicRQygZGhCCNCscEVUtHwJDNicoIJChYXGBkaJSYnKCkqNDU2Nzg5OkNERUZHSElKU1RV'
    'VldYWVpjZGVmZ2hpanN0dXZ3eHl6g4SFhoeIiYqSk5SVlpeYmZqio6Slpqeoqaqys7S1tre4ubrCw8TFxsfIycrS09TV1tfY2drh4uPk5ebn6Onq8fLz9PX29/j5+v/EAB8B'
    'AAMBAQEBAQEBAQEAAAAAAAABAgMEBQYHCAkKC//EALURAAIBAgQEAwQHBQQEAAECdwABAgMRBAUhMQYSQVEHYXETIjKBCBRCkaGxwQkjM1LwFWJy0QoWJDThJfEXGBkaJico'
    'KSo1Njc4OTpDREVGR0hJSlNUVVZXWFlaY2RlZmdoaWpzdHV2d3h5eoKDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXGx8jJytLT1NXW19jZ2uLj5'
    'OXm5+jp6vLz9PX29/j5+v/bAEMAAgICAgICAwICAwUDAwMFBgUFBQUGCAYGBgYGCAoICAgICAgKCgoKCgoKCgwMDAwMDA4ODg4ODw8PDw8PDw8PD//bAEMBAgICBAQEBwQE'
    'BxALCQsQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEP/dAAQAAf/aAAwDAQACEQMRAD8Ai8JfDH4bXPhXRri48J6TLLLZ'
    'W7u72EDMzNGpJJKZJJ6muh/4VX8MP+hQ0f8A8F9v/wDEVr+DP+RP0L/rwtf/AEUtdLX9AHln/9k='
)
GEOMETRY = '0,0,2,2'


def b64(raw):
    return base64.b64encode(raw).decode('ascii')


def jpeg_frame(change=b'', seed=42):
    comment = random.Random(seed).randbytes(61_000)
    comment = comment[:8000] + change + comment[8000:]
    return b64(JPEG[:2] + b'\xff\xfe' + struct.pack('!H', len(comment) + 2) + comment + JPEG[2:])


class CodecTests(unittest.TestCase):
    def test_dictionary_patch_is_lossless_and_smaller(self):
        base, target = jpeg_frame(), jpeg_frame(b'cursor moved')
        delta = encode_delta(base, target, 7)
        self.assertLess(len(delta), len(target) // 10)
        self.assertEqual(target, decode_delta(base, delta))
        self.assertIsNone(encode_delta(base, jpeg_frame(seed=123), 7))
        self.assertEqual(base, decode_delta(target, encode_delta(target, base, 8)))

    def test_rejects_invalid_size_bounds_markers_dictionary_and_deflate(self):
        base, target = jpeg_frame(), jpeg_frame(b'cursor moved')
        delta = encode_delta(base, target, 7)
        raw = base64.b64decode(delta)

        def patch_for(data, claimed_length, extra=b''):
            compressor = zlib.compressobj(wbits=-15)
            block = compressor.compress(data) + compressor.flush() + extra
            return HEADER.pack(claimed_length, hashlib.sha256(data).digest()) + BLOCK_LENGTH.pack(len(block)) + block

        cases = {
            'oversized base64': 'A' * (MAX_JPEG_BASE64 + 1),
            'invalid base64': '!',
            'missing header': b64(raw[:HEADER.size - 1]),
            'oversized output': b64(HEADER.pack(MAX_JPEG_BYTES + 1, bytes(32))),
            'truncated block': b64(raw[:-1]),
            'oversized block': b64(raw[:HEADER.size] + b'\xff\xff' + raw[HEADER.size + 2:]),
            'trailing patch bytes': b64(raw + b'garbage'),
            'trailing deflate bytes': b64(patch_for(JPEG, len(JPEG), b'garbage')),
            'too much expansion': b64(patch_for(b'X' * (BLOCK_SIZE + 1), BLOCK_SIZE)),
            'short output': b64(patch_for(JPEG, len(JPEG) + 1)),
            'missing SOI': b64(patch_for(JPEG[2:], len(JPEG) - 2)),
            'missing EOI': b64(patch_for(JPEG[:-2], len(JPEG) - 2)),
        }
        for name, invalid in cases.items():
            with self.subTest(name=name), self.assertRaises(DeltaError):
                decode_delta(base, invalid)
        with self.assertRaises(DeltaError):
            decode_delta(jpeg_frame(seed=123), delta)


class LiveDeltaTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        cert, key = root / 'relay.crt', root / 'relay.key'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-subj', '/CN=localhost', '-keyout', str(key), '-out', str(cert)],
                       check=True, capture_output=True)
        pin = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest()
        self.gateway = PeerGateway(root, cert, key, 0)
        self.addCleanup(self.gateway.hub.close)
        self.addCleanup(self.gateway.server_close)
        thread = threading.Thread(target=self.gateway.serve_forever, args=(.05,), daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.gateway.shutdown)
        endpoint = {'url': f'https://127.0.0.1:{self.gateway.server_port}', 'pin': pin}
        self.caller, self.target = (PinnedHub(endpoint, secrets.token_urlsafe(32)) for _ in range(2))
        for hub in (self.caller, self.target):
            self.addCleanup(lambda client=hub: client.connection and client.connection.close())
        self.caller.call('register', {'name': 'Viewer', 'platform': 'macos'})
        target = self.target.call('register', {'name': 'Capture', 'platform': 'windows', 'live_version': 1})
        self.target.call('availability', {'enabled': True})
        code = self.target.call('code')['code']
        approval = self.caller.call('connect', {'code': code})['approval']
        self.target.call('approve', {'approval': approval, 'allow': True})
        self.ident = self.caller.call('live-open', {'peer': target['id']})['id']

    def upload(self, seq, jpeg, geometry=GEOMETRY):
        frame = {'seq': seq, 'geometry': geometry, 'jpeg_base64': jpeg}
        response = self.target.call('live-frame', {'id': self.ident, 'frame': frame})
        self.assertEqual(seq, response['frame_seq'])

    def request(self, **body):
        return self.caller.call('live-view', {'id': self.ident, **body})['frame']

    def test_real_viewer_negotiates_recovers_jpeg_and_requests_full_after_bad_delta(self):
        images = [jpeg_frame(change) for change in (b'', b'cursor moved', b'cursor moved again')]
        self.upload(1, images[0])
        received = {}
        requests = []
        call = self.caller.call

        def recording_call(operation, body):
            requests.append(dict(body))
            response = call(operation, body)
            frame = response['frame']
            if frame:
                received.setdefault(frame['seq'], []).append(frame)
                if frame['seq'] == 3 and 'delta_base64' in frame:
                    return dict(response, frame=dict(frame, delta_base64='broken'))
            return response

        view = LiveView(self.caller, self.ident, threading.Event())

        def wait_for_frame(seq):
            with view.changed:
                self.assertTrue(view.changed.wait_for(
                    lambda: (view.value.get('frame') or {}).get('seq') == seq or view.value['error'],
                    timeout=3), view.snapshot())
                self.assertEqual('', view.value['error'])
                return view.value['frame']

        with patch.object(self.caller, 'call', side_effect=recording_call):
            view.start()
            try:
                self.assertEqual(images[0], wait_for_frame(1)['jpeg_base64'])
                self.assertIn('jpeg_base64', received[1][0])
                self.upload(2, images[1])
                self.assertEqual(images[1], wait_for_frame(2)['jpeg_base64'])
                self.assertEqual(1, received[2][0]['delta_after'])
                self.assertLess(len(received[2][0]['delta_base64']), len(images[1]) // 10)
                self.assertNotIn('delta_base64', view.snapshot()['frame'])
                self.upload(3, images[2])
                self.assertEqual(images[2], wait_for_frame(3)['jpeg_base64'])
                self.assertEqual(2, received[3][0]['delta_after'])
                self.assertIn('jpeg_base64', received[3][1])
                self.target.call('live-frame', {'id': self.ident, 'frame': {
                    'seq': 4, 'geometry': GEOMETRY, 'repeat_after': 3}})
                self.assertEqual(images[2], wait_for_frame(4)['jpeg_base64'])
                self.assertEqual(3, received[4][0]['repeat_after'])
            finally:
                view.close()
                view.thread.join(3)
        self.assertTrue(requests[0]['accept_delta'])
        self.assertTrue(any(req['after'] == 1 and req['after_geometry'] == GEOMETRY for req in requests))
        self.assertGreaterEqual(sum(req['after'] is None for req in requests), 2)

    def test_full_frame_on_legacy_viewer_bad_confirmation_gap_geometry_and_poor_savings(self):
        opts = {'accept_delta': True, 'after_geometry': GEOMETRY}
        self.upload(1, jpeg_frame())
        self.assertIn('jpeg_base64', self.request(after=0, **opts))
        self.upload(2, jpeg_frame(b'legacy'))
        self.assertIn('jpeg_base64', self.request(after=1))
        self.upload(3, jpeg_frame(b'wrong sequence'))
        self.assertIn('jpeg_base64', self.request(after=1, **opts))
        self.upload(4, jpeg_frame(b'wrong geometry'))
        self.assertIn('jpeg_base64', self.request(after=3, accept_delta=True, after_geometry='1,1,2,2'))
        self.upload(6, jpeg_frame(b'skipped capture'))
        patch = self.request(after=4, **opts)
        self.assertEqual(4, patch['delta_after'])
        self.assertEqual(jpeg_frame(b'skipped capture'),
                         decode_delta(jpeg_frame(b'wrong geometry'), patch['delta_base64']))
        self.upload(7, jpeg_frame(b'new geometry'), geometry='1,1,2,2')
        self.assertIn('jpeg_base64', self.request(after=6, **opts))
        self.upload(8, jpeg_frame(seed=123), geometry='1,1,2,2')
        self.assertIn('jpeg_base64', self.request(after=7, accept_delta=True, after_geometry='1,1,2,2'))

    def test_upload_continues_while_delta_is_compressed(self):
        self.upload(1, jpeg_frame())
        self.request()
        self.upload(2, jpeg_frame(b'cursor'))
        started, resume = threading.Event(), threading.Event()

        def slow_encode(*args):
            started.set()
            if not resume.wait(3):
                raise AssertionError('compression was not released')
            return encode_delta(*args)

        with ThreadPoolExecutor(max_workers=2) as workers, patch('lanpilot.peer_hub.encode_delta', slow_encode):
            download = workers.submit(self.request, after=1, after_geometry=GEOMETRY, accept_delta=True)
            try:
                self.assertTrue(started.wait(2))
                workers.submit(self.upload, 3, jpeg_frame(b'new capture')).result(timeout=2)
            finally:
                resume.set()
            self.assertEqual(2, download.result(timeout=2)['seq'])
        request = {'after': 2, 'after_geometry': GEOMETRY, 'accept_delta': True}
        self.assertEqual(2, self.request(**request)['delta_after'])
        # A retried request still acknowledges seq 2 after seq 3 was sent.
        self.assertIn('jpeg_base64', self.request(**request))


if __name__ == '__main__':
    unittest.main()
