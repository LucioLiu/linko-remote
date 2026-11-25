import base64
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from lanpilot.live import LiveSessions
from lanpilot.live_agent import LiveAgent
from lanpilot.live_view import LiveView


GEOMETRY = '0,0,100,100'
OTHER_GEOMETRY = '0,0,200,100'
JPEG = base64.b64encode(b'\xff\xd8fixture\xff\xd9').decode()
OTHER_JPEG = base64.b64encode(b'\xff\xd8changed\xff\xd9').decode()


class UploadRepeatTests(unittest.TestCase):
    def setUp(self):
        hub = SimpleNamespace(lock=threading.RLock(), has_pending_command=lambda *_: False,
                              _one=lambda *_: {'incoming': True, 'live': True})
        self.relay = LiveSessions(hub)
        self.now = 100
        self.ident = self.dispatch('live-open', 'viewer', peer='agent')['id']

    def dispatch(self, op, actor, **body):
        with self.relay.hub.lock:
            return self.relay.dispatch(op, actor, body, self.now)

    def upload(self, seq, geometry=GEOMETRY, **content):
        return self.dispatch('live-frame', 'agent', id=self.ident,
                             frame=dict(content, seq=seq, geometry=geometry))

    def view(self, **body):
        return self.dispatch('live-view', 'viewer', id=self.ident, **body)

    def test_repeat_refreshes_new_capture_and_composes_with_downlink_repeat(self):
        self.assertEqual(1, self.upload(1, jpeg_base64=JPEG)['frame_seq'])
        viewer = LiveView(None, self.ident, threading.Event())
        viewer.publish(self.view())
        self.now += 2.1
        self.assertIsNone(self.view()['frame'])

        self.assertEqual(3, self.upload(3, repeat_after=1)['frame_seq'])
        repeated = self.view(after=1, accept_repeat=True)
        self.assertEqual({'seq': 3, 'geometry': GEOMETRY, 'repeat_after': 1}, repeated['frame'])
        self.assertEqual(0, repeated['frame_age_ms'])
        viewer.publish(repeated)
        self.assertEqual(JPEG, viewer.value['frame']['jpeg_base64'])
        self.assertEqual(3, viewer.value['frame']['seq'])
        self.assertEqual(JPEG, self.view(after=1)['frame']['jpeg_base64'])

        self.now += 0.5
        self.assertEqual(5, self.upload(5, repeat_after=3)['frame_seq'])
        self.assertEqual(1, self.view(after=1, accept_repeat=True)['frame']['repeat_after'])
        self.assertEqual(6, self.upload(6, jpeg_base64=OTHER_JPEG)['frame_seq'])
        self.assertEqual(OTHER_JPEG, self.view(after=5, accept_repeat=True)['frame']['jpeg_base64'])

    def test_base_must_exist_and_match_current_sequence_and_geometry(self):
        self.assertIsNone(self.upload(2, repeat_after=1)['frame_seq'])
        self.assertIsNone(self.view()['frame'])
        self.upload(1, jpeg_base64=JPEG)
        self.upload(2, jpeg_base64=OTHER_JPEG)
        self.now += 1
        for bad_frame in ({'repeat_after': 1}, {'repeat_after': 2, 'geometry': OTHER_GEOMETRY}):
            with self.subTest(frame=bad_frame):
                self.assertIsNone(self.upload(3, **bad_frame)['frame_seq'])
                view = self.view()
                self.assertEqual(1000, view['frame_age_ms'])
                self.assertEqual(2, view['frame']['seq'])
                self.assertEqual(OTHER_JPEG, view['frame']['jpeg_base64'])

    def test_invalid_or_retried_upload_does_not_refresh_relay_timestamp(self):
        self.upload(1, jpeg_base64=JPEG)
        self.upload(2, repeat_after=1)
        self.now += 1
        for invalid in ({'repeat_after': True}, {'repeat_after': 2}, {'repeat_after': 1, 'jpeg_base64': JPEG}):
            with self.subTest(frame=invalid), self.assertRaises(ValueError):
                self.upload(2, **invalid)
        self.assertIsNone(self.upload(2, repeat_after=1)['frame_seq'])
        self.assertIsNone(self.upload(2, jpeg_base64=OTHER_JPEG)['frame_seq'])
        self.assertEqual(2, self.upload(2, jpeg_base64=JPEG)['frame_seq'])
        self.assertEqual(1000, self.view()['frame_age_ms'])


class AgentUploadRepeatTests(unittest.TestCase):
    def run_uploads(self, captures, responses, platform='win32'):
        sent = []
        acknowledged = []
        captures = iter(captures)
        responses = iter(responses)
        stop = threading.Event()

        def call(operation, body):
            self.assertEqual('live-frame', operation)
            sent.append(body['frame'])
            response = next(responses)
            if isinstance(response, Exception):
                raise response
            return {'active': True, **response}

        def read(_):
            acknowledged.append((agent.last_frame, agent.media_at))
            capture = next(captures, None)
            if capture is None:
                stop.set()
                return {}
            seq, geometry, jpeg, created = capture
            return {'id': 'session', 'seq': seq, 'geometry': geometry, 'jpeg_base64': jpeg, 'created': created}

        client = SimpleNamespace(config={'endpoint': {}, 'credential': ''},
                                 stop_event=stop, agent=SimpleNamespace(workspace=Path('.')))
        with patch('lanpilot.live_agent.sys.platform', platform):
            agent = LiveAgent(client, lambda *_: SimpleNamespace(call=call), lambda *_: None)
        agent.current = 'session'
        agent.read = read
        with patch.object(stop, 'wait'), patch('lanpilot.live_agent.time.time', return_value=100):
            agent.frames()
        self.assertFalse(agent.error)
        return sent, acknowledged

    def test_only_fresh_new_windows_captures_with_same_bytes_and_geometry_repeat(self):
        captures = [
            (1, GEOMETRY, JPEG, 100),
            (1, GEOMETRY, JPEG, 100),
            (2, GEOMETRY, JPEG, 98),
            (3, GEOMETRY, JPEG, 100),
            (4, OTHER_GEOMETRY, JPEG, 100),
            (5, OTHER_GEOMETRY, OTHER_JPEG, 100),
        ]
        sent, media = self.run_uploads(captures, [{'frame_seq': seq} for seq in (1, 3, 4, 5)])
        self.assertEqual([
            {'seq': 1, 'geometry': GEOMETRY, 'jpeg_base64': JPEG},
            {'seq': 3, 'geometry': GEOMETRY, 'repeat_after': 1},
            {'seq': 4, 'geometry': OTHER_GEOMETRY, 'jpeg_base64': JPEG},
            {'seq': 5, 'geometry': OTHER_GEOMETRY, 'jpeg_base64': OTHER_JPEG},
        ], sent)
        self.assertEqual([0, 1, 1, 1, 3, 4, 5], [seq for seq, _ in media])
        self.assertEqual(media[1], media[2])
        self.assertEqual(media[1], media[3])
        self.assertGreater(media[4][1], media[3][1])

    def test_old_relay_and_non_windows_sender_use_full_frames(self):
        captures = [(seq, GEOMETRY, JPEG, 100) for seq in (1, 2)]
        for platform, responses in (('win32', [{}, {}]), ('darwin', [{'frame_seq': 1}, {'frame_seq': 2}])):
            with self.subTest(platform=platform):
                sent, media = self.run_uploads(captures, responses, platform)
                self.assertEqual([JPEG, JPEG], [frame['jpeg_base64'] for frame in sent])
                self.assertEqual(2, media[-1][0])

    def test_base_mismatch_and_lost_response_retry_latest_capture_in_full(self):
        captures = [(seq, GEOMETRY, JPEG, 100) for seq in (1, 2, 3, 4, 4, 5)]
        sent, media = self.run_uploads(captures, [
            {'frame_seq': 1}, {'frame_seq': None}, {'frame_seq': 3},
            OSError('response lost'), {'frame_seq': 4}, {'frame_seq': 5},
        ])
        self.assertEqual([1, 2, 3, 4, 4, 5], [frame['seq'] for frame in sent])
        self.assertEqual([None, 1, None, 3, None, 4], [frame.get('repeat_after') for frame in sent])
        self.assertEqual(JPEG, sent[2]['jpeg_base64'])
        self.assertEqual(JPEG, sent[4]['jpeg_base64'])
        self.assertEqual(media[1], media[2])
        self.assertEqual(media[3], media[4])
        self.assertEqual(5, media[-1][0])
