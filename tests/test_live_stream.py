"""Loopback tests of the real product viewer and relay over pinned TLS."""
import base64
import hashlib
import json
from pathlib import Path
import secrets
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from lanpilot.peer_client import DesktopClient, HubRejection, PinnedHub, RelayIdentityError
from lanpilot.peer_gateway import Handler, PeerGateway


GEOMETRY = '0,0,1280,720'


class RecordingHandler(Handler):
    def do_POST(self):
        self.server.paths.append(self.path)
        if self.path == '/v2/live-stream' and self.server.reject_stream:
            self.close_connection = True
            return self.answer(404, {'ok': False, 'error': 'Not found'})
        return super().do_POST()

    def answer(self, status, value):
        if self.path == '/v2/live-open' and self.server.legacy and value.get('ok'):
            value['value'].pop('view_stream')
        super().answer(status, value)

    def stream_chunk(self, record):
        value = json.loads(record) if record != b'\n' else None
        if value and self.server.pause_before == (value.get('frame') or {}).get('seq'):
            self.server.write_paused.set()
            self.server.continue_writing.wait(3)
        super().stream_chunk(record)
        if value:
            self.server.records.append(value)
            if self.server.pause_after == (value.get('frame') or {}).get('seq'):
                self.server.write_paused.set()
                self.server.continue_writing.wait(2)


class LiveStreamTLSTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        home = Path(directory.name)
        cert, key = home / 'cert.pem', home / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-subj', '/CN=localhost', '-keyout', str(key), '-out', str(cert)],
                       check=True, capture_output=True)
        pin = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest()
        self.relay = PeerGateway(home, cert, key, 0)
        self.relay.RequestHandlerClass = RecordingHandler
        self.relay.paths = []
        self.relay.records = []
        self.relay.legacy = False
        self.relay.reject_stream = False
        self.relay.pause_after = -1
        self.relay.pause_before = -1
        self.relay.write_paused = threading.Event()
        self.relay.continue_writing = threading.Event()
        self.addCleanup(self.relay.hub.close)
        self.addCleanup(self.relay.server_close)
        thread = threading.Thread(target=self.relay.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.relay.shutdown)
        self.addCleanup(self.relay.continue_writing.set)
        self.endpoint = {'url': f'https://127.0.0.1:{self.relay.server_port}', 'pin': pin}
        self.client = DesktopClient(home / 'viewer', self.endpoint)
        self.addCleanup(self.close_viewer)
        self.controller = self.client.live_input_hub
        self.controller.call('register', {'name': 'Viewer', 'platform': self.client.platform})
        self.sender = PinnedHub(self.endpoint, secrets.token_urlsafe(32))
        self.addCleanup(self.close_hub, self.sender)
        self.target = self.sender.call('register', {'name': 'Windows', 'platform': 'windows', 'live_version': 1})['id']
        self.sender.call('availability', {'enabled': True})
        code = self.sender.call('code')['code']
        approval = self.controller.call('connect', {'code': code})['approval']
        self.sender.call('approve', {'approval': approval, 'allow': True})

    @staticmethod
    def close_hub(hub):
        if hub.connection:
            hub.connection.close()

    def close_viewer(self):
        if self.client.live_view:
            self.client.live_view.close()
            self.client.live_view.thread.join(1)
        self.close_hub(self.client.live_hub)
        self.close_hub(self.client.live_input_hub)

    def wait_for(self, predicate, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.01)
        self.fail('Timed out waiting for live media state')

    def open_view(self):
        session = self.client.api('live-open', {'peer': self.target, 'viewer_transport': 'stream'})
        return session, self.client.live_view

    def send(self, identifier, seq, jpeg=b'fixture', repeat_after=None):
        frame = {'geometry': GEOMETRY, 'seq': seq}
        if repeat_after is None:
            frame['jpeg_base64'] = base64.b64encode(b'\xff\xd8' + jpeg + b'\xff\xd9').decode()
        else:
            frame['repeat_after'] = repeat_after
        return self.sender.call('live-frame', {'id': identifier, 'frame': frame})

    def test_default_viewer_uses_confirmed_frame_deltas(self):
        session = self.client.api('live-open', {'peer': self.target})
        self.send(session['id'], 1)
        self.wait_for(lambda: self.client.live_view.snapshot()['frame'] is not None)
        self.assertIn('/v2/live-view', self.relay.paths)
        self.assertNotIn('/v2/live-stream', self.relay.paths)

    def test_product_stream_coalesces_old_frames_and_reuses_static_jpeg(self):
        self.relay.pause_after = 1
        session, view = self.open_view()
        self.assertEqual(1, session['view_stream'])
        identifier = session['id']
        self.send(identifier, 1)
        self.assertTrue(self.relay.write_paused.wait(1))
        self.wait_for(lambda: (view.snapshot()['frame'] or {}).get('seq') == 1)
        original = view.value['frame']['jpeg_base64']
        self.send(identifier, 2, repeat_after=1)
        self.send(identifier, 3, repeat_after=2)
        self.relay.continue_writing.set()
        self.wait_for(lambda: (view.snapshot()['frame'] or {}).get('seq') == 3)
        self.assertEqual(original, view.value['frame']['jpeg_base64'])
        self.assertEqual({'seq': 3, 'geometry': GEOMETRY, 'repeat_after': 1},
                         self.relay.records[-1]['frame'])
        self.send(identifier, 4, jpeg=b'changed')
        self.wait_for(lambda: (view.snapshot()['frame'] or {}).get('seq') == 4)
        self.assertIn('jpeg_base64', self.relay.records[-1]['frame'])
        self.assertNotIn('/v2/live-view', self.relay.paths)
        self.sender.call('availability', {'enabled': False})
        self.assertTrue(view.stop.wait(1))
        self.assertTrue(view.snapshot()['error'])
        self.assertNotIn('/v2/live-view', self.relay.paths)

    def test_viewer_disconnect_unblocks_relay_and_stream_records_cannot_renew_lease(self):
        now = 1000
        self.relay.hub.clock = lambda: now
        session, view = self.open_view()
        identifier = session['id']
        self.send(identifier, 1)
        self.wait_for(lambda: view.snapshot()['frame'] is not None)
        live = self.relay.hub.live.sessions[identifier]
        now += 7
        self.send(identifier, 2, repeat_after=1)
        self.wait_for(lambda: (view.snapshot()['frame'] or {}).get('seq') == 2)
        self.assertEqual(1000, live['viewed'])
        self.assertEqual(1000, self.sender.call('live-exchange', {'id': identifier})['lease_ms'])
        now += 1
        self.assertFalse(self.sender.call('live-exchange', {'id': identifier})['active'])
        view.close()
        view.thread.join(1)
        self.assertFalse(view.thread.is_alive())
        self.wait_for(lambda: not live['streaming'], timeout=1)

    def test_live_client_renews_on_next_batch(self):
        patcher = patch('lanpilot.peer_gateway.LIVE_STREAM_SECONDS', .5)
        patcher.start()
        self.addCleanup(patcher.stop)
        session, view = self.open_view()
        identifier = session['id']
        self.send(identifier, 1)
        self.wait_for(lambda: view.snapshot()['frame'] is not None)
        live = self.relay.hub.live.sessions[identifier]
        first_view = live['viewed']
        self.wait_for(lambda: live['viewed'] > first_view and self.relay.paths.count('/v2/live-stream') >= 2,
                      timeout=5)
        self.send(identifier, 2, repeat_after=1)
        self.wait_for(lambda: (view.snapshot()['frame'] or {}).get('seq') == 2)
        self.assertEqual({'seq': 2, 'geometry': GEOMETRY, 'repeat_after': 1}, self.relay.records[-1]['frame'])
        self.assertNotIn('/v2/live-view', self.relay.paths)

    def test_relay_without_stream_capability_uses_live_view(self):
        self.relay.legacy = True
        session, view = self.open_view()
        self.assertNotIn('view_stream', session)
        self.send(session['id'], 1)
        self.wait_for(lambda: view.snapshot()['frame'] is not None)
        self.assertIn('/v2/live-view', self.relay.paths)
        self.assertNotIn('/v2/live-stream', self.relay.paths)

    def test_queued_frame_preserves_input_freshness_limit(self):
        self.relay.pause_before = 1
        session, view = self.open_view()
        self.send(session['id'], 1)
        self.assertTrue(self.relay.write_paused.wait(1))
        time.sleep(1.55)
        self.relay.continue_writing.set()
        self.wait_for(lambda: view.value['frame'] is not None)
        self.assertIsNone(view.snapshot()['frame'])
        self.assertGreater(view.snapshot()['frame_age_ms'], 1500)

    def test_unsupported_stream_endpoint_falls_back(self):
        self.relay.reject_stream = True
        session, view = self.open_view()
        self.send(session['id'], 1)
        self.wait_for(lambda: view.snapshot()['frame'] is not None)
        self.assertEqual(1, self.relay.paths.count('/v2/live-stream'))
        self.assertIn('/v2/live-view', self.relay.paths)

    def test_stream_checks_pin_and_viewer_identity(self):
        identifier = self.controller.call('live-open', {'peer': self.target})['id']
        with self.assertRaises(HubRejection):
            self.sender.live_stream(identifier).run(lambda: True, self.fail)
        requests = len(self.relay.paths)
        wrong_pin = PinnedHub(dict(self.endpoint, pin='0' * 64), self.controller.credential)
        with self.assertRaises(RelayIdentityError):
            wrong_pin.live_stream(identifier).run(lambda: True, self.fail)
        self.assertEqual(requests, len(self.relay.paths))
