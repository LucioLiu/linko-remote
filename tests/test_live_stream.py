"""Loopback tests of the real product viewer and relay over pinned TLS."""
import base64
import hashlib
import json
from pathlib import Path
import secrets
import ssl
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from lanpilot.live import AVC_BATCH_BYTES, AVC_BATCH_FRAMES, AVC_MAX_HISTORY_BYTES, AVC_MAX_PAYLOAD
from lanpilot.live_agent import LiveAgent
from lanpilot.peer_client import DesktopClient, HubRejection, PinnedConnection, PinnedHub, RelayIdentityError, atomic_json
from lanpilot.peer_gateway import Handler, PeerGateway


GEOMETRY = '0,0,1280,720'
AVC_GEOMETRY = '-1280,-20,1280,720'
# Two 64x64 Baseline GOPs encoded with Annex B and repeat headers.
ACCESS_UNITS = tuple(map(base64.b64decode, (
    'AAAAAWdCwAraEJoQAAADABAAAAMCiPEiagAAAAFozg/IAAAAAWWIhDomKAAITMnJyddddddddddddeA=',
    'AAAAAUGaIDa8ZFAMUAxQDFAMUAxQDFAMUAxQDFAMUAxQDFAMUAxQDFAOJ8T4nz+fz+fz+fz+fz+fz+fw',
    'AAAAAWdCwAraEJoQAAADABAAAAMCiPEiagAAAAFozg/IAAAAAWWIggKomKAAIqcnJyddddddddddddeA',
    'AAAAAUGaIDa8ZFAMUAxQDFAMUAxQDFAMUAxQDFAMUAxQDFAMUAxQDFAOJ8T4nz+fz+fz+fz+fz+fz+fw',
)))


def avc_upload(seq, unit, *, age=0, flags=None, limit=None, tick=None):
    payload = ACCESS_UNITS[unit]
    if limit is not None:
        # Fill the record to its wire limit with a filler-data NAL.
        payload += b'\x00\x00\x00\x01\x0c' + b'\xff' * (limit - len(payload) - 6) + b'\x80'
    if tick is None:
        tick = 8500 + seq * 50
    header = struct.pack('<4sIQQHHIiiII', b'LPA1', len(payload), seq, tick,
                         64, 64, int(unit % 2 == 0) if flags is None else flags, -1280, -20, 1280, 720)
    return {'avc_base64': base64.b64encode(header + payload).decode(), 'capture_age_ms': age}


class RecordingHandler(Handler):
    def do_POST(self):
        self.server.paths.append(self.path)
        if self.path == '/v2/live-avc-upload' and self.server.reject_upload:
            self.close_connection = True
            return self.answer(503, {'ok': False, 'error': 'Upload unavailable'})
        if self.path == '/v2/live-stream' and self.server.reject_stream:
            self.close_connection = True
            return self.answer(404, {'ok': False, 'error': 'Not found'})
        return super().do_POST()

    def answer(self, status, value):
        if self.path == '/v2/live-open' and self.server.legacy and value.get('ok'):
            value['value'].pop('view_stream')
        if self.path == '/v2/live-open' and self.server.legacy_view_batch and value.get('ok'):
            value['value'].pop('avc_view_batch', None)
        if not self.server.stream_upload and value.get('ok'):
            value['value'].pop('avc_upload', None)
            for session in value['value'].get('sessions', []):
                session.pop('avc_upload', None)
        if self.server.legacy_upload and value.get('ok'):
            value['value'].pop('avc_batch', None)
            for session in value['value'].get('sessions', []):
                session.pop('avc_batch', None)
        if (self.path == '/v2/live-exchange' and self.server.hold_progress
                and value.get('value', {}).get('upload_seen')):
            self.server.progress_blocked.set()
            self.server.release_progress.wait(3)
        if self.path == '/v2/live-avc-batch' and self.server.hold_upload:
            self.server.upload_committed.set()
            self.server.release_upload.wait(3)
            if self.server.lose_upload_ack:
                self.close_connection = True
                return
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
        self.home = home
        self.relay.legacy = False
        self.relay.legacy_view_batch = False
        self.relay.legacy_upload = False
        self.relay.stream_upload = False
        self.relay.reject_upload = False
        self.relay.hold_progress = False
        self.relay.progress_blocked = threading.Event()
        self.relay.release_progress = threading.Event()
        self.relay.hold_upload = False
        self.relay.lose_upload_ack = False
        self.relay.upload_committed = threading.Event()
        self.relay.release_upload = threading.Event()
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


    def open_avc(self):
        self.sender.call('register', {'name': 'Windows', 'platform': 'windows', 'live_version': 1, 'live_avc': 1})
        session = self.controller.call('live-open', {'peer': self.target, 'accept_avc': 1})
        self.assertEqual(1, session['live_avc'])
        self.assertEqual({'id': session['id'], 'live_avc': 1, 'need_idr': True},
                         self.sender.call('live-agent-state')['sessions'][0])
        return session['id']

    def avc_stream(self, identifier, *, connection=None, after=None, hub=None, accept_avc=1, bounded=False):
        hub = hub or self.controller
        if connection is None:
            connection = PinnedConnection(hub.host, hub.port, hub.pin)
            self.addCleanup(connection.close)
        headers = {'Authorization': 'Bearer ' + hub.credential, 'Content-Type': 'application/json'}
        request = {'id': identifier, 'after': after, 'accept_avc': accept_avc}
        if bounded:
            request['avc_view_batch'] = 1
        body = json.dumps(request)
        connection.request('POST', '/v2/live-stream', body, headers)
        response = connection.getresponse()
        self.addCleanup(response.close)
        return connection, response

    @staticmethod
    def read_update(response, *, media_only=True):
        for raw in response:
            if raw == b'\n':
                continue
            value = json.loads(raw)
            if not media_only or value['frame'] or value['error']:
                return value
        raise AssertionError('No live update before the batch ended')

    def upload_avc(self, identifier, frame, hub=None):
        return (hub or self.sender).call('live-frame', {'id': identifier, 'frame': frame})

    def test_avc_requires_opt_in_on_both_sides(self):
        session = self.controller.call('live-open', {'peer': self.target, 'accept_avc': 1})
        self.assertNotIn('live_avc', session)
        with self.assertRaises(HubRejection):
            self.upload_avc(session['id'], avc_upload(1, 0))
        self.controller.call('live-close', {'id': session['id']})
        self.sender.call('register', {'name': 'Windows', 'platform': 'windows', 'live_version': 1, 'live_avc': 1})
        legacy = self.controller.call('live-open', {'peer': self.target})
        self.assertNotIn('live_avc', legacy)
        self.send(legacy['id'], 1)
        self.assertIn('jpeg_base64', self.controller.call('live-view', {'id': legacy['id']})['frame'])

    def test_new_avc_viewer_uses_legacy_stream_without_bounded_capability(self):
        self.relay.legacy_view_batch = True
        self.sender.call('register', {'name': 'Windows', 'platform': 'windows',
                                      'live_version': 1, 'live_avc': 1})
        session = self.client.api('live-open', {'peer': self.target, 'viewer_transport': 'stream', 'accept_avc': 1})
        self.assertNotIn('avc_view_batch', session)
        self.upload_avc(session['id'], avc_upload(1, 0, age=500))
        # The old stream still delivers an IDR older than the new relay-side selection limit.
        self.wait_for(lambda: (self.client.live_view.snapshot()['frame'] or {}).get('seq') == 1)
        self.assertIn('/v2/live-stream', self.relay.paths)
        self.assertNotIn('/v2/live-view', self.relay.paths)

    def test_bounded_view_renews_with_confirmed_cursor_and_requests_fresh_idr(self):
        self.sender.call('register', {'name': 'Windows', 'platform': 'windows',
                                      'live_version': 1, 'live_avc': 1})
        session = self.controller.call('live-open', {'peer': self.target, 'accept_avc': 1, 'avc_view_batch': 1})
        self.assertEqual(1, session['avc_view_batch'])
        identifier = session['id']
        self.upload_avc(identifier, avc_upload(1, 0, age=500))
        _, forbidden = self.avc_stream(identifier, hub=self.sender, bounded=True)
        self.assertEqual(403, forbidden.status)

        connection, response = self.avc_stream(identifier, bounded=True)
        with response:
            updates = [json.loads(line) for line in response if line != b'\n']
        self.assertTrue(updates)
        self.assertTrue(all(value['frame'] is None for value in updates))
        self.assertTrue(self.sender.call('live-exchange', {'id': identifier})['need_idr'])
        self.upload_avc(identifier, avc_upload(2, 0))
        self.upload_avc(identifier, avc_upload(3, 1))
        with patch('lanpilot.peer_gateway.AVC_VIEW_BATCH_BYTES', 1):
            for after, expected in ((None, 2), (2, 3)):
                _, response = self.avc_stream(identifier, connection=connection, after=after, bounded=True)
                with response:
                    updates = [json.loads(line) for line in response if line != b'\n']
                self.assertEqual([expected], [value['frame']['seq'] for value in updates if value['frame']])
        self.sender.call('availability', {'enabled': False})
        _, revoked = self.avc_stream(identifier, connection=connection, after=3, bounded=True)
        self.assertEqual(403, revoked.status)

    def test_avc_preserves_native_bytes_and_resumes_only_on_idr_after_uplink_gap(self):
        identifier = self.open_avc()
        with self.assertRaises(HubRejection):
            self.upload_avc(identifier, avc_upload(1, 0), hub=self.controller)
        _, forbidden = self.avc_stream(identifier, hub=self.sender)
        self.assertEqual(403, forbidden.status)
        _, response = self.avc_stream(identifier)
        self.assertEqual(200, response.status)
        self.assertTrue(response.chunked)

        self.assertTrue(self.upload_avc(identifier, avc_upload(1, 1))['need_idr'])
        for seq, unit in ((2, 0), (3, 1)):
            frame = avc_upload(seq, unit)
            self.assertEqual(seq, self.upload_avc(identifier, frame)['frame_seq'])
            self.assertEqual({'seq': seq, 'geometry': AVC_GEOMETRY, 'idr': unit == 0,
                              'avc_base64': frame['avc_base64']}, self.read_update(response)['frame'])

        missing = self.upload_avc(identifier, avc_upload(5, 1))
        self.assertIsNone(missing['frame_seq'])
        self.assertTrue(missing['need_idr'])
        self.assertTrue(self.sender.call('live-exchange', {'id': identifier})['need_idr'])
        self.assertIsNone(self.upload_avc(identifier, avc_upload(4, 1))['frame_seq'])
        self.assertIsNone(self.upload_avc(identifier, avc_upload(6, 1))['frame_seq'])
        self.assertFalse(self.upload_avc(identifier, avc_upload(7, 2))['need_idr'])
        self.upload_avc(identifier, avc_upload(8, 3))
        self.assertEqual([7, 8], [self.read_update(response)['frame']['seq'] for _ in range(2)])
        self.sender.call('availability', {'enabled': False})
        revoked = self.read_update(response)
        self.assertTrue(revoked['error'])
        self.assertIsNone(revoked['frame'])
        with self.assertRaises(HubRejection):
            self.upload_avc(identifier, avc_upload(9, 0))

    def start_windows_upload(self, identifier):
        workspace = self.home / 'worker'
        workspace.mkdir()
        client = SimpleNamespace(config={'endpoint': self.endpoint, 'credential': self.sender.credential},
                                 incoming=True, stop_event=threading.Event(),
                                 agent=SimpleNamespace(workspace=workspace))
        agent = LiveAgent(client, PinnedHub, atomic_json)
        agent.avc_enabled = True
        self.addCleanup(self.close_hub, agent.hub)
        self.addCleanup(self.close_hub, agent.frame_hub)
        fixture_clock = patch('lanpilot.live_agent.windows_tick_ms', return_value=9800)
        fixture_clock.start()
        self.addCleanup(fixture_clock.stop)
        workers = [threading.Thread(target=entry, daemon=True) for entry in (agent.run, agent.frames)]
        for worker in workers:
            worker.start()
            self.addCleanup(worker.join, 4)
        self.addCleanup(client.stop_event.set)
        self.addCleanup(self.relay.release_upload.set)
        self.addCleanup(self.relay.release_progress.set)
        self.wait_for(lambda: agent.read('live-control.json').get('live_avc') == 1)

        def capture(seq, unit, *, batch=True):
            tick = 9700 + seq
            record = avc_upload(seq, unit, tick=tick)['avc_base64']
            mailbox = f'live-avc-{seq % AVC_BATCH_FRAMES}.json' if batch else 'live-frame.json'
            atomic_json(workspace / mailbox, {
                'id': identifier, 'seq': seq, 'capture_tick': tick,
                'idr': unit % 2 == 0, 'avc_base64': record,
            })
            return record

        return agent, capture

    def test_stream_sender_continues_in_order_without_progress_reply(self):
        self.relay.stream_upload = self.relay.hold_progress = True
        identifier = self.open_avc()
        _, response = self.avc_stream(identifier)
        agent, capture = self.start_windows_upload(identifier)
        self.assertTrue(agent.avc_stream)
        capture(1, 0)
        self.assertEqual(1, self.read_update(response)['frame']['seq'])
        self.assertTrue(self.relay.progress_blocked.wait(1))
        for seq in range(2, 5):
            capture(seq, 1)
        self.assertEqual([2, 3, 4], [self.read_update(response)['frame']['seq'] for _ in range(3)])
        self.assertEqual(0, agent.avc_seq)
        self.assertEqual(1, self.relay.paths.count('/v2/live-avc-upload'))
        self.assertNotIn('/v2/live-avc-batch', self.relay.paths)
        self.relay.release_progress.set()
        self.wait_for(lambda: agent.avc_seq == 4)
        self.assertFalse(agent.avc_pending)
        self.sender.call('availability', {'enabled': False})
        self.assertTrue(self.read_update(response)['error'])
        self.wait_for(lambda: agent.read('live-control.json').get('expires') == 0)

    def test_stream_rejection_falls_back_to_batch_with_fresh_idr(self):
        self.relay.stream_upload = self.relay.reject_upload = True
        identifier = self.open_avc()
        _, response = self.avc_stream(identifier)
        agent, capture = self.start_windows_upload(identifier)
        capture(1, 0)
        self.wait_for(lambda: agent.read('live-control.json').get('avc_key', 0) >= 1)
        self.assertFalse(agent.avc_stream)
        capture(3, 1)
        self.wait_for(lambda: agent.avc_seen == 3)
        capture(4, 2)
        frame = self.read_update(response)['frame']
        self.assertEqual((4, True), (frame['seq'], frame['idr']))
        self.assertIn('/v2/live-avc-batch', self.relay.paths)

    def test_upload_endpoint_checks_sender_and_control_lease(self):
        now = 1000
        self.relay.hub.clock = lambda: now
        self.relay.stream_upload = True
        identifier = self.open_avc()
        lease = {'id': identifier, 'accept_avc_batch': 1, 'accept_avc_upload': 1}
        premature = self.sender.avc_upload(identifier)
        self.addCleanup(premature.close)
        premature.open()
        self.assertEqual(403, premature.connection.getresponse().status)

        self.sender.call('live-exchange', lease)
        foreign = self.controller.avc_upload(identifier)
        self.addCleanup(foreign.close)
        foreign.open()
        self.assertEqual(403, foreign.connection.getresponse().status)

        sender = self.sender.avc_upload(identifier)
        self.addCleanup(sender.close)
        sender.open()
        sender.send(avc_upload(1, 0))
        session = self.relay.hub.live.sessions[identifier]
        self.wait_for(lambda: session.get('upload_seq') == 1)
        duplicate = self.sender.avc_upload(identifier)
        self.addCleanup(duplicate.close)
        duplicate.open()
        self.assertEqual(403, duplicate.connection.getresponse().status)

        now += 2.5
        sender.send(avc_upload(2, 1))
        self.assertEqual(403, sender.connection.getresponse().status)
        self.assertEqual(1, session['upload_seq'])
        self.assertEqual(1, session['upload_seen'])

    def test_legacy_batch_fences_an_open_upload_stream(self):
        self.relay.stream_upload = True
        identifier = self.open_avc()
        self.sender.call('live-exchange', {'id': identifier, 'accept_avc_batch': 1, 'accept_avc_upload': 1})
        sender = self.sender.avc_upload(identifier)
        self.addCleanup(sender.close)
        sender.open()
        sender.send(avc_upload(1, 0))
        session = self.relay.hub.live.sessions[identifier]
        self.wait_for(lambda: session.get('upload_seq') == 1)
        self.assertEqual(3, self.sender.call(
            'live-avc-batch', {'id': identifier, 'frames': [avc_upload(3, 2)]})['frame_seq'])
        sender.send(avc_upload(4, 3))
        with sender.connection.getresponse() as response:
            self.assertEqual(403, response.status)
        self.assertEqual(3, session['avc'].last_seq)

    def test_upload_stream_rejects_non_avc_records(self):
        self.relay.stream_upload = True
        identifier = self.open_avc()
        self.sender.call('live-exchange', {'id': identifier, 'accept_avc_batch': 1, 'accept_avc_upload': 1})
        sender = self.sender.avc_upload(identifier)
        self.addCleanup(sender.close)
        sender.open()
        sender.send({'geometry': AVC_GEOMETRY, 'seq': 1, 'jpeg_base64': '/9j/2Q=='})
        with sender.connection.getresponse() as response:
            self.assertEqual(400, response.status)
        session = self.relay.hub.live.sessions[identifier]
        self.assertEqual(0, session['avc'].last_seq)
        self.wait_for(lambda: session.get('upload_retired'))

    def test_stream_limits_pending_frames_bytes_and_age(self):
        agent = LiveAgent(SimpleNamespace(config={'endpoint': self.endpoint, 'credential': self.sender.credential},
                                         incoming=True, stop_event=threading.Event(),
                                         agent=SimpleNamespace(workspace=self.home)), PinnedHub, atomic_json)
        agent.current = 'session'
        agent.avc_active = agent.avc_stream = True
        agent.avc_lease = time.monotonic() + 3
        sent = []
        upload = SimpleNamespace(send=sent.append)
        def frame(seq, size=1, age=0, idr=False):
            return {'seq': seq, 'avc_base64': 'a' * size, 'capture_tick': 1000 - age, 'idr': idr}

        with patch('lanpilot.live_agent.windows_tick_ms', return_value=1000):
            agent.read_avc_mailbox = lambda _: [frame(1, AVC_MAX_PAYLOAD * 4 // 3, idr=True),
                                                 frame(2, AVC_MAX_PAYLOAD)]
            agent.send_avc_stream('session', upload)
            self.assertEqual(1, len(sent))
            self.assertEqual(1, agent.avc_seen)

            agent.update_avc_stream('session', {'avc_upload': 1, 'upload_seen': 1, 'upload_seq': 1})
            agent.read_avc_mailbox = lambda _: [frame(seq) for seq in range(2, 32)]
            agent.send_avc_stream('session', upload)
            self.assertEqual(24, len(agent.avc_pending))
            self.assertEqual(25, len(sent))
            agent.update_avc_stream('session', {'avc_upload': 1, 'upload_seen': 25, 'upload_seq': 25})
            agent.read_avc_mailbox = lambda _: [frame(26, age=300), frame(27), frame(28, idr=True)]
            agent.send_avc_stream('session', upload)
            self.assertEqual(28, agent.avc_sent)
            self.assertEqual(26, len(sent))
            self.assertEqual(1, agent.avc_key)
            agent.read_avc_mailbox = lambda _: []
            elapsed = agent.avc_pending[0][1] + 2
            with patch('lanpilot.live_agent.time.monotonic', return_value=elapsed):
                agent.send_avc_stream('session', upload)
            self.assertFalse(agent.avc_stream)
            self.assertFalse(agent.avc_pending)

    def test_windows_batch_mailbox_pressure_lost_ack_and_receipts_over_tls(self):
        identifier = self.open_avc()
        _, response = self.avc_stream(identifier)
        agent, capture = self.start_windows_upload(identifier)
        self.assertEqual(1, agent.read('live-control.json')['avc_batch'])
        self.relay.hold_upload = self.relay.lose_upload_ack = True
        capture(7, 0)
        self.assertTrue(self.relay.upload_committed.wait(2))
        self.assertEqual(7, self.read_update(response)['frame']['seq'])

        # Overflow all eight slots while the first media ACK is blocked.
        for seq in range(8, 18):
            capture(seq, 1)
        self.assertEqual(AVC_BATCH_FRAMES, len(list(agent.root.glob('live-avc-*.json'))))
        self.assertEqual(1, self.relay.paths.count('/v2/live-avc-batch'))
        self.assertEqual({'accepted': 1}, self.controller.call('live-input', {
            'id': identifier, 'seq': 1, 'geometry': AVC_GEOMETRY, 'events': [{'type': 'release'}],
        }))
        self.wait_for(lambda: agent.read(f'{identifier}-0000000001.live-input.json').get('seq') == 1)
        atomic_json(agent.root / 'live-receipt.json', {'id': identifier, 'seq': 1})
        self.wait_for(lambda: self.relay.hub.live.sessions[identifier]['ack'] == 1)
        self.assertEqual(1, self.relay.paths.count('/v2/live-avc-batch'))

        self.relay.release_upload.set()
        self.wait_for(lambda: agent.read('live-control.json').get('avc_key') == 1)
        self.wait_for(lambda: agent.avc_seen == 17)
        self.assertEqual(1, self.relay.paths.count('/v2/live-avc-batch'))
        self.relay.lose_upload_ack = False
        self.relay.release_upload.clear()
        self.relay.upload_committed.clear()
        reference = capture(18, 2)
        self.assertTrue(self.relay.upload_committed.wait(2))
        frame = self.read_update(response)['frame']
        self.assertEqual((18, reference, True), (frame['seq'], frame['avc_base64'], frame['idr']))
        resumed = [capture(seq, 3) for seq in range(19, 22)]
        self.relay.hold_upload = False
        self.relay.release_upload.set()
        for seq, record in zip(range(19, 22), resumed):
            frame = self.read_update(response)['frame']
            self.assertEqual((seq, record, AVC_GEOMETRY),
                             (frame['seq'], frame['avc_base64'], frame['geometry']))
        self.wait_for(lambda: agent.avc_seq == 21)
        self.assertEqual(3, self.relay.paths.count('/v2/live-avc-batch'))

        self.sender.call('availability', {'enabled': False})
        self.wait_for(lambda: agent.read('live-control.json').get('expires') == 0)
        requests = self.relay.paths.count('/v2/live-avc-batch')
        capture(22, 0)
        self.assertTrue(self.read_update(response)['error'])
        self.assertEqual(requests, self.relay.paths.count('/v2/live-avc-batch'))

    def test_windows_upload_uses_serial_avc_when_legacy_relay_omits_batch(self):
        identifier = self.open_avc()
        self.relay.legacy_upload = True
        _, response = self.avc_stream(identifier)
        agent, capture = self.start_windows_upload(identifier)
        self.assertNotIn('avc_batch', agent.read('live-control.json'))
        capture(7, 0, batch=False)
        self.assertEqual(7, self.read_update(response)['frame']['seq'])
        self.assertIn('/v2/live-frame', self.relay.paths)
        self.assertNotIn('/v2/live-avc-batch', self.relay.paths)

    def test_avc_upload_batch_validates_all_records_before_publishing(self):
        identifier = self.open_avc()

        def upload(*records, hub=self.sender):
            return hub.call('live-avc-batch', {'id': identifier, 'frames': list(records)})

        key = avc_upload(1, 0)
        p_frame = avc_upload(2, 1)
        # The frames fit the total HTTP envelope but exceed its AVC payload budget.
        oversized = AVC_BATCH_BYTES * 3 // 8
        for invalid in ((key, p_frame, dict(key, avc_base64='***')), (p_frame, key),
                        tuple(avc_upload(seq, 0) for seq in range(1, AVC_BATCH_FRAMES + 2)),
                        (avc_upload(1, 0, limit=oversized), avc_upload(2, 1, limit=oversized))):
            with self.assertRaises(HubRejection):
                upload(*invalid)
        with self.assertRaises(HubRejection):
            upload(key, hub=self.controller)
        history = self.relay.hub.live.sessions[identifier]['avc']
        self.assertFalse(history.frames)
        self.assertEqual(2, upload(key, p_frame)['frame_seq'])
        stale = upload(avc_upload(3, 0, age=1500), avc_upload(4, 1))
        self.assertIsNone(stale['frame_seq'])
        self.assertTrue(stale['need_idr'])
        self.assertFalse(history.frames)
        changed = avc_upload(6, 1)
        binary = bytearray(base64.b64decode(changed['avc_base64']))
        struct.pack_into('<i', binary, 32, -1200)
        changed['avc_base64'] = base64.b64encode(binary).decode()
        self.assertTrue(upload(avc_upload(5, 0), changed)['need_idr'])
        self.assertFalse(history.frames)
        self.assertEqual(7, upload(avc_upload(7, 0))['frame_seq'])

    def test_avc_batch_cursor_requires_same_tls_connection_and_confirmed_sequence(self):
        identifier = self.open_avc()
        with patch('lanpilot.peer_gateway.LIVE_STREAM_SECONDS', .4):
            connection, first = self.avc_stream(identifier)
            self.upload_avc(identifier, avc_upload(1, 0))
            self.assertEqual(1, self.read_update(first)['frame']['seq'])
            first.read()
            self.upload_avc(identifier, avc_upload(2, 1))
            _, renewed = self.avc_stream(identifier, connection=connection, after=1)
            self.assertEqual(2, self.read_update(renewed)['frame']['seq'])
            renewed.read()
            _, unconfirmed = self.avc_stream(identifier, connection=connection, after=1)
            self.assertTrue(self.read_update(unconfirmed)['frame']['idr'])
            unconfirmed.read()
            _, reconnected = self.avc_stream(identifier, after=2)
            self.assertEqual(1, self.read_update(reconnected)['frame']['seq'])

    def test_avc_history_limits_bytes_and_frames_and_signals_new_idr(self):
        identifier = self.open_avc()
        self.upload_avc(identifier, avc_upload(1, 0, limit=AVC_MAX_PAYLOAD))
        self.upload_avc(identifier, avc_upload(2, 1, limit=AVC_MAX_PAYLOAD))
        history = self.relay.hub.live.sessions[identifier]['avc']
        self.assertEqual([2], [frame['seq'] for frame, _ in history.frames])
        self.assertLessEqual(history.size, AVC_MAX_HISTORY_BYTES)
        _, response = self.avc_stream(identifier, after=1)
        self.assertIsNone(self.read_update(response, media_only=False)['frame'])
        self.assertTrue(self.sender.call('live-exchange', {'id': identifier})['need_idr'])

        with patch('lanpilot.live.AVC_MAX_HISTORY_FRAMES', 2):
            self.upload_avc(identifier, avc_upload(3, 1))
            self.upload_avc(identifier, avc_upload(4, 1))
            self.assertEqual([3, 4], [frame['seq'] for frame, _ in history.frames])
        fresh = avc_upload(5, 2, limit=AVC_MAX_PAYLOAD)
        self.upload_avc(identifier, fresh)
        self.assertEqual(fresh['avc_base64'], self.read_update(response)['frame']['avc_base64'])

    def test_avc_rejects_bad_records_and_preserves_freshness_and_lease(self):
        now = 1000
        self.relay.hub.clock = lambda: now
        identifier = self.open_avc()
        for bad in (avc_upload(1, 1, flags=1), avc_upload(1, 0, flags=3),
                    avc_upload(1, 0, limit=AVC_MAX_PAYLOAD + 1),
                    dict(avc_upload(1, 0), avc_base64='***'),
                    dict(avc_upload(1, 0), capture_age_ms=-1)):
            with self.assertRaises(HubRejection):
                self.upload_avc(identifier, bad)

        self.assertIsNone(self.upload_avc(identifier, avc_upload(1, 0, age=1500))['frame_seq'])
        key = avc_upload(2, 0, age=800)
        self.assertEqual(2, self.upload_avc(identifier, key)['frame_seq'])
        _, response = self.avc_stream(identifier)
        self.assertGreaterEqual(self.read_update(response)['frame_age_ms'], 800)
        now += .8
        self.assertEqual(2, self.upload_avc(identifier, key)['frame_seq'])
        request = {'id': identifier, 'seq': 1, 'geometry': AVC_GEOMETRY,
                   'events': [{'type': 'move', 'x': 10, 'y': 20}]}
        with self.assertRaises(HubRejection):
            self.controller.call('live-input', request)

        self.assertEqual(3, self.upload_avc(identifier, avc_upload(3, 1))['frame_seq'])
        self.assertEqual({'accepted': 1}, self.controller.call('live-input', request))
        self.assertEqual(1, self.sender.call('live-exchange', {'id': identifier})['inputs'][0]['seq'])
        self.assertEqual(3, self.read_update(response)['frame']['seq'])
        self.assertTrue(self.sender.call('live-exchange', {'id': identifier, 'ack': 1})['active'])
        self.assertEqual(1, self.read_update(response, media_only=False)['ack'])
        now = 1008
        stopped = self.upload_avc(identifier, avc_upload(4, 2))
        self.assertFalse(stopped['active'])
        self.assertIsNone(stopped['frame_seq'])

    def test_avc_sender_can_fall_back_to_jpeg_on_existing_stream(self):
        identifier = self.open_avc()
        _, response = self.avc_stream(identifier)
        self.upload_avc(identifier, avc_upload(1, 0))
        self.assertTrue(self.read_update(response)['frame']['idr'])
        self.assertEqual(0, self.send(identifier, 1)['live_avc'])
        self.assertIn('jpeg_base64', self.read_update(response)['frame'])
        self.assertEqual(0, self.sender.call('live-exchange', {'id': identifier})['live_avc'])
        self.assertIsNone(self.upload_avc(identifier, avc_upload(2, 0))['frame_seq'])

    def test_avc_viewer_can_fall_back_to_jpeg_polling(self):
        identifier = self.open_avc()
        self.upload_avc(identifier, avc_upload(1, 0))
        self.assertIsNone(self.controller.call('live-view', {'id': identifier})['frame'])
        self.assertEqual(0, self.sender.call('live-exchange', {'id': identifier})['live_avc'])
        self.send(identifier, 1)
        self.assertIn('jpeg_base64', self.controller.call('live-view', {'id': identifier})['frame'])

    def test_avc_stream_without_opt_in_never_sends_buffered_avc(self):
        identifier = self.open_avc()
        self.upload_avc(identifier, avc_upload(1, 0))
        _, response = self.avc_stream(identifier, accept_avc=0)
        self.assertEqual(0, self.sender.call('live-exchange', {'id': identifier})['live_avc'])
        self.send(identifier, 1)
        self.assertIn('jpeg_base64', self.read_update(response)['frame'])

    def test_avc_jpeg_fallback_before_first_idr(self):
        identifier = self.open_avc()
        _, response = self.avc_stream(identifier)
        self.assertEqual(0, self.send(identifier, 1)['live_avc'])
        self.assertIn('jpeg_base64', self.read_update(response)['frame'])

    def test_avc_slow_upload_counts_toward_capture_age(self):
        identifier = self.open_avc()
        connection = PinnedConnection(self.sender.host, self.sender.port, self.sender.pin)
        self.addCleanup(connection.close)
        body = json.dumps({'id': identifier, 'frame': avc_upload(1, 0, age=1400)}).encode()
        connection.putrequest('POST', '/v2/live-frame')
        connection.putheader('Authorization', 'Bearer ' + self.sender.credential)
        connection.putheader('Content-Length', str(len(body)))
        connection.endheaders(body[:-1])
        self.wait_for(lambda: self.relay.paths.count('/v2/live-frame') == 1)
        time.sleep(.15)
        connection.send(body[-1:])
        with connection.getresponse() as response:
            result = json.load(response)['value']
        self.assertTrue(result['active'])
        self.assertTrue(result['need_idr'])
        self.assertIsNone(result['frame_seq'])
