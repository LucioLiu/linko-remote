import json
from pathlib import Path
import secrets
import tempfile
import unittest
import uuid

from lanpilot.peer_hub import PeerHub


class PeerHubTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.now = 10000.0
        self.hub = PeerHub(Path(self.folder.name) / 'hub.sqlite', lambda: self.now)
        self.a, self.b, self.c = [secrets.token_urlsafe(32) for _ in range(3)]
        self.devices = {}
        for token, name, system in [(self.a, 'Mac', 'macos'), (self.b, 'Windows PC', 'windows'), (self.c, 'Third PC', 'windows')]:
            self.devices[token] = self.call('register', token, name=name, platform=system)['id']

    def tearDown(self):
        self.hub.close()
        self.folder.cleanup()

    def call(self, operation, credential, **body):
        return self.hub.dispatch(operation, credential, body)

    def pair(self, caller, target):
        self.call('availability', target, enabled=True)
        code = self.call('code', target)['code']
        approval = self.call('connect', caller, code=code)['approval']
        self.call('approve', target, approval=approval, allow=True)
        return approval

    def submit(self, caller, target, identifier=None):
        return self.call('submit', caller, peer=self.devices[target], id=identifier or uuid.uuid4().hex,
                         request={'action': 'observe'})['id']

    def test_both_platforms_can_approve_and_control(self):
        self.pair(self.a, self.b)
        self.pair(self.b, self.a)
        self.assertTrue(self.call('state', self.a)['peers'][0]['can_control'])
        self.assertTrue(self.call('state', self.b)['peers'][0]['can_control'])
        self.assertEqual(self.devices[self.b], self.call('state', self.a)['peers'][0]['id'])

    def test_desktop_approval_does_not_grant_file_jobs(self):
        self.pair(self.a, self.b)
        with self.assertRaisesRegex(PermissionError, '仅允许'):
            self.call('submit', self.a, peer=self.devices[self.b], id=uuid.uuid4().hex,
                      request={'action':'job','manifest':{}})

    def test_unknown_recovery_blocks_any_later_submission_of_that_input(self):
        self.pair(self.a, self.b)
        identifier = uuid.uuid4().hex
        result = self.call('recover', self.a, id=identifier)
        self.assertEqual('complete', result['state'])
        self.assertFalse(result['result']['input_attempted'])
        self.assertEqual('REQUEST_NOT_ACCEPTED_CANCELLED', result['result']['error'])
        with self.assertRaises(PermissionError):
            self.submit(self.a, self.b, identifier)
        self.assertIsNone(self.call('poll', self.b))
        self.assertEqual(result, self.call('recover', self.a, id=identifier))

    def test_pairing_does_not_grant_reverse_control(self):
        self.pair(self.a, self.b)
        self.assertFalse(self.call('state', self.b)['peers'][0]['can_control'])
        with self.assertRaises(PermissionError):
            self.submit(self.b, self.a)

    def test_long_term_trust_survives_pause_and_can_be_revoked(self):
        self.call('availability', self.b, enabled=True)
        code = self.call('code', self.b)['code']
        approval = self.call('connect', self.a, code=code)['approval']
        self.call('approve', self.b, approval=approval, allow=True, persistent=True)
        self.now += 7200
        self.assertTrue(self.call('state', self.a)['peers'][0]['can_control'])
        self.assertFalse(self.call('state', self.b)['peers'][0]['can_control'])
        self.call('availability', self.b, enabled=False)
        self.assertTrue(self.call('state', self.b)['peers'][0]['trusted_incoming'])
        self.assertFalse(self.call('state', self.a)['peers'][0]['can_control'])
        with self.assertRaises(PermissionError):
            self.submit(self.a, self.b)
        self.call('availability', self.b, enabled=True)
        self.assertTrue(self.call('state', self.a)['peers'][0]['can_control'])
        self.call('disconnect', self.b, peer=self.devices[self.a])
        with self.assertRaises(PermissionError):
            self.submit(self.a, self.b)

    def test_target_must_approve_each_new_session(self):
        self.call('availability', self.b, enabled=True)
        code = self.call('code', self.b)['code']
        approval = self.call('connect', self.a, code=code)['approval']
        with self.assertRaises(PermissionError):
            self.submit(self.a, self.b)
        with self.assertRaises(PermissionError):
            self.call('approve', self.c, approval=approval, allow=True)
        self.call('approve', self.b, approval=approval, allow=False)
        with self.assertRaises(PermissionError):
            self.submit(self.a, self.b)

    def test_code_is_short_lived_single_use_and_refresh_revokes_old(self):
        self.call('availability', self.b, enabled=True)
        old = self.call('code', self.b)['code']
        new = self.call('code', self.b)['code']
        with self.assertRaises(PermissionError):
            self.call('connect', self.a, code=old)
        self.call('connect', self.a, code=new)
        with self.assertRaises(PermissionError):
            self.call('connect', self.c, code=new)
        newer = self.call('code', self.b)['code']
        self.now += 601
        with self.assertRaises(PermissionError):
            self.call('connect', self.c, code=newer)

    def test_invalid_attempts_remain_rate_limited_after_rollback(self):
        for _ in range(10):
            with self.assertRaises(PermissionError):
                self.call('connect', self.a, code='000000000000')
        with self.assertRaisesRegex(PermissionError, '频繁'):
            self.call('connect', self.a, code='000000000000')

    def test_credentials_not_stored_or_returned_to_other_peers(self):
        self.pair(self.a, self.b)
        state = json.dumps(self.call('state', self.a))
        self.assertNotIn(self.b, state)
        rows = self.hub.db.execute('SELECT credential FROM devices').fetchall()
        self.assertNotIn(self.b, [row['credential'] for row in rows])

    def test_receipts_are_immutable_and_recover_never_redelivers(self):
        self.pair(self.a, self.b)
        identifier = self.submit(self.a, self.b)
        command = self.call('poll', self.b)
        self.assertEqual(identifier, command['id'])
        self.assertIsNone(self.call('poll', self.b))
        self.assertEqual('unconfirmed', self.call('recover', self.a, id=identifier)['state'])
        receipt = {'request_id': identifier, 'ok': True, 'input_attempted': False, 'result': {'frame_id': 'f' * 32}}
        with self.assertRaises(PermissionError):
            self.call('result', self.c, id=identifier, result=receipt)
        self.call('result', self.b, id=identifier, result=receipt)
        self.call('result', self.b, id=identifier, result=receipt)
        with self.assertRaises(PermissionError):
            self.call('result', self.b, id=identifier, result=dict(receipt, ok=False))
        self.assertEqual(receipt, self.call('recover', self.a, id=identifier)['result'])
        self.assertEqual(identifier, self.submit(self.a, self.b, identifier))
        self.assertIsNone(self.call('poll', self.b))

    def test_unknown_command_blocks_other_controllers_for_same_target(self):
        self.pair(self.a, self.b)
        self.pair(self.c, self.b)
        self.submit(self.a, self.b)
        with self.assertRaisesRegex(ValueError, '未确认'):
            self.submit(self.c, self.b)

    def test_input_expired_while_offline_recovers_without_waiting_for_target(self):
        self.pair(self.a, self.b)
        identifier = uuid.uuid4().hex
        self.call('submit', self.a, peer=self.devices[self.b], id=identifier,
                  request={'action': 'click', 'frame_id': 'f' * 32, 'x': 1, 'y': 1, 'button': 'left'})
        self.now += 46
        recovered = self.call('recover', self.a, id=identifier)
        self.assertEqual('complete', recovered['state'])
        self.assertFalse(recovered['result']['input_attempted'])
        self.assertEqual('EXPIRED_OR_ACCESS_REVOKED_BEFORE_DELIVERY', recovered['result']['error'])
        self.assertIsNone(self.call('poll', self.b))

    def test_expired_status_does_not_block_another_controller(self):
        self.pair(self.a, self.b)
        self.pair(self.c, self.b)
        identifier = uuid.uuid4().hex
        self.call('submit', self.a, peer=self.devices[self.b], id=identifier, request={'action': 'status'})
        self.assertEqual(identifier, self.call('poll', self.b)['id'])
        self.now += 46
        replacement = self.submit(self.c, self.b)
        self.assertEqual(replacement, self.call('poll', self.b)['id'])
        recovered = self.call('recover', self.a, id=identifier)
        self.assertEqual('complete', recovered['state'])
        self.assertEqual('STATUS_TIMED_OUT', recovered['result']['error'])
        late_result = {'request_id': identifier, 'ok': True, 'input_attempted': False}
        self.assertTrue(self.call('result', self.b, id=identifier, result=late_result)['saved'])
        self.assertEqual(recovered, self.call('recover', self.a, id=identifier))

    def test_uncertain_delivered_input_blocks_only_its_target(self):
        self.pair(self.a, self.b)
        self.pair(self.a, self.c)
        identifier = uuid.uuid4().hex
        self.call('submit', self.a, peer=self.devices[self.b], id=identifier,
                  request={'action': 'click', 'frame_id': 'f' * 32, 'x': 1, 'y': 1, 'button': 'left'})
        self.assertEqual(identifier, self.call('poll', self.b)['id'])
        self.now += 46
        self.assertEqual('unconfirmed', self.call('recover', self.a, id=identifier)['state'])
        with self.assertRaisesRegex(ValueError, '未确认'):
            self.submit(self.a, self.b)
        other = self.submit(self.a, self.c)
        self.assertEqual(other, self.call('poll', self.c)['id'])
        self.assertIsNone(self.call('poll', self.b))

    def test_revoke_before_delivery_prevents_input_and_new_commands(self):
        self.pair(self.a, self.b)
        identifier = self.submit(self.a, self.b)
        self.call('availability', self.b, enabled=False)
        self.assertIsNone(self.call('poll', self.b))
        receipt = self.call('recover', self.a, id=identifier)['result']
        self.assertFalse(receipt['input_attempted'])
        with self.assertRaises(PermissionError):
            self.submit(self.a, self.b)

    def test_queue_and_receipt_survive_hub_restart(self):
        self.pair(self.a, self.b)
        identifier = self.submit(self.a, self.b)
        self.hub.close()
        self.hub = PeerHub(Path(self.folder.name) / 'hub.sqlite', lambda: self.now)
        self.assertEqual(identifier, self.call('poll', self.b)['id'])


if __name__ == '__main__':
    unittest.main()
