import base64
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lanpilot.live_agent import LiveAgent
from lanpilot.peer_client import DesktopClient, HubRejection, PinnedHub


def avc_frame(seq, *, idr=False, tick=990):
    payload = (b'\x00\x00\x01\x67s\x00\x00\x01\x68p\x00\x00\x01\x65k' if idr
               else b'\x00\x00\x01\x41p')
    header = struct.pack('<4sIQQHHIiiII', b'LPA1', len(payload), seq, tick, 640, 480,
                         int(idr), -100, 0, 800, 600)
    return dict(id='session', seq=seq, idr=idr, capture_tick=tick,
                avc_base64=base64.b64encode(header + payload).decode())


class WindowsAvcUploadTests(unittest.TestCase):
    def setUp(self):
        self.stop = threading.Event()
        client = SimpleNamespace(config={'endpoint': {}, 'credential': ''},
                                 stop_event=self.stop, incoming=True,
                                 agent=SimpleNamespace(workspace=Path('.')))
        self.writes = []
        self.sent = []
        self.replies = []
        self.agent = LiveAgent(client, lambda *_: SimpleNamespace(call=self.call),
                               lambda path, body: self.writes.append((path.name, body)))
        self.agent.current = 'session'
        self.agent.avc_active = True
        self.agent.avc_lease = time.monotonic() + 5
        self.agent.read = lambda _: {}

    def call(self, op, body=None):
        self.sent.append((op, body))
        response = self.replies.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def upload(self, frame):
        with patch('lanpilot.live_agent.windows_tick_ms', return_value=1000):
            self.agent.upload_avc('session', [frame])

    def test_upload_wire_shape_and_wait_for_idr_after_missing_sequence(self):
        first = avc_frame(1, idr=True)
        next_key = avc_frame(5, idr=True)
        self.replies = [{'active': True, 'frame_seq': seq, 'need_idr': False} for seq in (1, 2, 5, 6)]
        for frame in (first, avc_frame(2), avc_frame(4), next_key, avc_frame(6)):
            self.upload(frame)
        self.assertEqual(4, len(self.sent))
        self.assertEqual(('live-frame', {'id': 'session', 'frame': {
            'avc_base64': first['avc_base64'], 'capture_age_ms': 10,
        }}), self.sent[0])
        self.assertEqual(1, self.agent.avc_key)
        self.assertFalse(self.agent.avc_waiting_key)
        self.assertEqual(6, self.agent.avc_seq)
        self.assertEqual(0, self.agent.last_frame)

    def test_relay_key_requests_and_lost_responses_require_new_reference(self):
        self.replies = [
            {'active': True, 'frame_seq': 1, 'need_idr': True},
            OSError('acknowledgement lost'),
            {'active': True, 'frame_seq': None, 'need_idr': True},
            {'active': True, 'frame_seq': 7, 'need_idr': False},
        ]
        for seq in range(1, 8):
            self.upload(avc_frame(seq, idr=seq in (1, 3, 5, 7)))
        self.assertEqual(4, len(self.sent))
        self.assertEqual(3, self.agent.avc_key)
        self.assertEqual(7, self.agent.avc_seq)

    def test_capture_must_be_fresh_and_authorized_at_upload(self):
        self.upload(avc_frame(1, idr=True, tick=0))
        self.agent.avc_lease = 0
        self.upload(avc_frame(2, idr=True))
        self.assertEqual([], self.sent)
        self.assertEqual(1, self.agent.avc_key)
        self.assertEqual(1, self.agent.avc_seen)

    def test_rejected_avc_can_fall_back_to_jpeg_with_its_own_sequence(self):
        self.replies = [HubRejection('unsupported or invalid AVC', 400),
                        {'active': True, 'frame_seq': 1}]
        self.upload(avc_frame(8, idr=True))
        self.assertFalse(self.agent.avc_active)
        self.agent.upload_frame('session', {'seq': 1, 'geometry': '0,0,800,600', 'jpeg_base64': 'jpeg'})
        self.assertEqual({'seq': 1, 'geometry': '0,0,800,600', 'jpeg_base64': 'jpeg'},
                         self.sent[1][1]['frame'])
        self.assertEqual(1, self.agent.last_frame)

    def test_helper_and_both_negotiation_responses_gate_worker_mode(self):
        for built, advertised, accepted, expected in (
            (True, True, True, True),
            (False, True, True, False),
            (True, False, True, False),
            (True, True, False, False),
        ):
            with self.subTest(built=built, advertised=advertised, accepted=accepted):
                self.stop.clear()
                self.agent.expire()
                self.writes.clear()
                self.agent.avc_enabled = built
                entry = {'id': 'session', **({'live_avc': 1} if advertised else {})}
                exchange = {'active': True, 'lease_ms': 2500, 'inputs': []}
                if accepted:
                    exchange['live_avc'] = 1
                self.replies = [{'sessions': [entry]}, exchange, dict(exchange, live_avc=0), exchange]
                waits = iter((False, False, True))

                def wait(_):
                    if next(waits):
                        self.stop.set()
                    return self.stop.is_set()

                with patch.object(self.stop, 'wait', side_effect=wait):
                    self.agent.run()
                first, downgrade, later, revoke = [value for _, value in self.writes]
                self.assertEqual(expected, first.get('live_avc') == 1)
                self.assertNotIn('live_avc', downgrade)
                self.assertNotIn('live_avc', later)
                self.assertEqual({'id': 'session', 'expires': 0}, revoke)

    def test_registration_advertises_avc_only_with_windows_binary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            helper = root / 'windows/video/lanpilot-avc.exe'
            helper.parent.mkdir(parents=True)
            registration = []

            def call(op, body=None):
                if op == 'register':
                    registration.append(body)
                    client.stop_event.set()
                return {}

            for idx, (platform, helper_exists, expected) in enumerate((
                    ('windows', False, False), ('windows', True, True), ('macos', True, False))):
                with self.subTest(platform=platform, helper_exists=helper_exists):
                    if helper_exists:
                        helper.touch()
                    with patch('lanpilot.peer_client.ROOT', root), \
                         patch('lanpilot.peer_client.desktop_platform', return_value=platform):
                        client = DesktopClient(root / str(idx), {}, lambda *_: SimpleNamespace(call=call),
                                               lambda home: SimpleNamespace(workspace=home, permissions=lambda: {}))
                    with patch.object(client, '_reconcile_incoming'), patch.object(client, '_sync_commands'):
                        client._pump()
                    self.assertEqual(1, registration[-1]['live_version'])
                    self.assertEqual(expected, registration[-1].get('live_avc') == 1)

    def test_avc_transport_never_retries_same_capture(self):
        hub = PinnedHub({'url': 'https://example.test', 'pin': 'a' * 64}, 'credential')
        with patch.object(hub, '_call', side_effect=OSError('acknowledgement lost')) as call:
            with self.assertRaises(OSError):
                hub.call('live-frame', {'frame': {'avc_base64': 'frame', 'capture_age_ms': 10}})
            self.assertEqual(1, call.call_count)
