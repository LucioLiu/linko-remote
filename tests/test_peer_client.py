import hashlib
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import http.client
import http.server
import json
import os
from pathlib import Path
import secrets
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import uuid

from lanpilot.peer_client import (
    DesktopClient, HubRejection, InstanceLock, LocalHandler, MailboxAgent, PinnedHub, RelayIdentityError, atomic_json,
    default_home,
)
from lanpilot.peer_gateway import Handler, PeerGateway
from lanpilot.peer_hub import PeerHub


class LocalTransport:
    hub = None

    def __init__(self, endpoint, credential):
        self.credential = credential
        self.calls = []
        self.drop_submit = False

    def call(self, operation, body=None):
        self.calls.append(operation)
        try:
            value = self.hub.dispatch(operation, self.credential, body or {})
        except PermissionError as error:
            if operation == 'submit':
                raise HubRejection(str(error)) from error
            raise
        if operation == 'submit' and self.drop_submit:
            raise OSError('response was lost after submission')
        return value


class FakeAgent:
    def __init__(self, home):
        self.workspace = Path(home) / 'worker'
        self.workspace.mkdir(exist_ok=True)
        self.enabled = False

    def start(self):
        self.enabled = True

    def stop(self):
        self.enabled = False

    def running(self):
        return self.enabled

    def permissions(self):
        return {'screen': True, 'input': True}


class ClientTests(unittest.TestCase):
    def setUp(self):
        platform = patch('lanpilot.peer_client.desktop_platform', return_value='macos')
        platform.start()
        self.addCleanup(platform.stop)
        self.folder = tempfile.TemporaryDirectory()
        self.hub = PeerHub(Path(self.folder.name) / 'hub.sqlite')
        LocalTransport.hub = self.hub
        self.endpoint = {'url': 'https://hub.example:443', 'pin': 'a' * 64}
        self.clients = []
        for name in ('one', 'two'):
            client = DesktopClient(Path(self.folder.name) / name, self.endpoint, LocalTransport, FakeAgent)
            client.agent_hub.call('register', {'name': name, 'platform': client.platform})
            self.clients.append(client)
        self.a, self.b = self.clients
        self.b.api('availability', {'enabled': True})
        code = self.b.api('code', {})['connection']
        pending = self.a.api('connect', {'code': code})
        self.b.api('approve', {'approval': pending['approval'], 'allow': True})
        self.target = self.b.agent_hub.call('state')['self']['id']

    def tearDown(self):
        self.hub.close()
        self.folder.cleanup()

    def test_fresh_install_has_new_identity_empty_contacts_and_no_os_hostname(self):
        with patch('lanpilot.peer_client.socket.gethostname', return_value='private-hostname'):
            fresh = DesktopClient(Path(self.folder.name) / 'fresh', self.endpoint,
                                  LocalTransport, FakeAgent)
        self.assertNotIn('private-hostname', fresh.config['name'])
        self.assertNotEqual(self.a.config['credential'], fresh.config['credential'])
        self.assertNotEqual(self.b.config['credential'], fresh.config['credential'])
        self.assertEqual([], fresh.config['ssh'])
        self.assertFalse(fresh.incoming)
        fresh.agent_hub.call('register', {'name': fresh.config['name'], 'platform': fresh.platform})
        view = fresh.agent_hub.call('state')
        self.assertEqual([], view['peers'])
        self.assertEqual([], view['approvals'])
        self.assertEqual([], view['outgoing'])
        self.assertFalse(view['incoming'])
        self.assertNotIn(self.target, json.dumps(view))
        self.assertNotIn(self.a.config['credential'], json.dumps(view))

    def test_update_preferences_preserve_existing_identity_name_and_ssh(self):
        self.a.config['name'] = 'Saved PC'
        self.a.config['ssh'] = [{'id': 'saved', 'name': 'Saved host', 'host': 'host.example', 'port': 22, 'user': ''}]
        before = dict(self.a.config)
        self.a.api('update-preference', {'enabled': False})
        restored = json.loads(self.a.path.read_text())
        self.assertFalse(restored['auto_update_check'])
        self.assertEqual(before, {key: value for key, value in restored.items() if key != 'auto_update_check'})
        self.assertFalse(self.a.local_state()['updates']['auto_check'])

    def test_update_install_is_blocked_by_sessions_or_unconfirmed_actions(self):
        with patch.object(self.b.updater, 'prepare_install') as install:
            with self.assertRaisesRegex(ValueError, '结束远程'):
                self.b.api('update-install', {})
            install.assert_not_called()
        self.a.state['peers'] = [{'can_control': True}]
        with patch.object(self.a.updater, 'prepare_install') as install:
            with self.assertRaises(ValueError):
                self.a.api('update-install', {})
            self.a.state['peers'] = []
            self.a.pending['peer'] = 'a' * 32
            with self.assertRaises(ValueError):
                self.a.api('update-install', {})
            install.assert_not_called()
            self.a.pending.clear()
            install.return_value = {'installing': True}
            self.assertTrue(self.a.api('update-install', {})['installing'])
            self.assertTrue(self.a.updating)
            with self.assertRaisesRegex(ValueError, '正在更新'):
                self.a.api('name', {'name': 'Changed during update'})

    def test_complete_connection_codes_validate_service_identity(self):
        import base64
        value = dict(v=2, code='123456789012', url='https://impostor.example', pin='b' * 64)
        code = 'linko2:' + base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip('=')
        with self.assertRaisesRegex(ValueError, '不同'):
            self.a.api('connect', {'code': code})

    def test_lost_submit_response_is_recovered_without_second_input(self):
        self.a.command_hub.drop_submit = True
        value = self.a.api('remote', {'peer': self.target, 'request': {'action': 'observe'}})
        self.assertEqual('unconfirmed', value['state'])
        with self.assertRaisesRegex(ValueError, '未确认'):
            self.a.api('remote', {'peer': self.target, 'request': {'action': 'observe'}})
        self.assertEqual(1, self.a.command_hub.calls.count('submit'))
        command = self.b.agent_hub.call('poll')
        receipt = {'request_id': command['id'], 'ok': True, 'input_attempted': False,
                   'result': {'frame_id': 'a' * 32}}
        self.b.agent_hub.call('result', {'id': command['id'], 'result': receipt})
        recovered = self.a.api('recover', {'id': command['id']})
        self.assertEqual('complete', recovered['state'])
        self.assertEqual({}, self.a.pending)

    def test_input_mailbox_publishes_once_after_submission_and_result_replies_are_lost(self):
        self.a.command_hub.drop_submit = True
        payload = {'peer': self.target, 'request': {'action': 'text', 'text': 'fixture', 'frame_id': 'f' * 32}}
        submitted = self.a.api('remote', payload)
        identifier = submitted['id']
        self.assertEqual('unconfirmed', submitted['state'])

        self.b.agent = MailboxAgent(self.b.home)
        workspace = self.b.agent.workspace
        with patch.object(self.b.agent, 'running', return_value=True), \
             patch.object(self.b.agent, 'execute', wraps=self.b.agent.execute) as execute:
            self.b._sync_commands()
            request = workspace / (identifier + '.request.json')
            self.assertEqual('text', json.loads(request.read_text())['action'])
            request.rename(workspace / (identifier + '.executing'))
            self.assertEqual('unconfirmed', self.a.api('recover', {'id': identifier})['state'])
            self.b._sync_commands()
            self.assertFalse(request.exists())

            receipt = {'request_id': identifier, 'ok': True, 'input_attempted': True}
            atomic_json(workspace / (identifier + '.response.json'), receipt)
            transport = self.b.agent_hub.call

            def drop_saved_result(operation, body):
                transport(operation, body)
                raise OSError('Fixture reply lost after saving result')

            with patch.object(self.b.agent_hub, 'call', side_effect=drop_saved_result):
                with self.assertRaises(OSError):
                    self.b._sync_commands()
            self.b._sync_commands()
            self.b._sync_commands()
            execute.assert_called_once()
            self.assertFalse(request.exists())
        for _ in range(2):
            self.assertEqual(receipt, self.a.api('recover', {'id': identifier})['result'])
        self.assertEqual({}, self.a.local_state()['pending'])
        self.assertEqual(1, self.a.command_hub.calls.count('submit'))

    def test_rejected_submission_clears_pending_and_preserves_rejection(self):
        self.a.api('disconnect', {'peer': self.target})
        payload = {'peer': self.target, 'request': {'action': 'observe'}}
        with self.assertRaisesRegex(HubRejection, '尚未取得对方授权'):
            self.a.api('remote', payload)
        self.assertEqual({}, self.a.pending)
        self.assertEqual({}, json.loads(self.a.pending_path.read_text()))
        self.assertNotIn('recover', self.a.command_hub.calls)

        approval = self.a.api('access', {'peer': self.target})['approval']
        self.b.api('approve', {'approval': approval, 'allow': True})
        submitted = self.a.api('remote', payload)
        self.assertEqual(submitted['id'], self.b.agent_hub.call('poll')['id'])

    def test_unsaved_pending_is_not_published_or_submitted(self):
        payload = {'peer': self.target, 'request': {'action': 'observe'}}
        with patch('lanpilot.peer_client.atomic_json', side_effect=OSError('Fixture disk write failure')):
            with self.assertRaises(OSError):
                self.a.api('remote', payload)
        self.assertEqual({}, self.a.local_state()['pending'])
        self.assertNotIn('submit', self.a.command_hub.calls)

    def test_certificate_rejection_before_submit_does_not_leave_false_pending(self):
        payload = {'peer': self.target, 'request': {'action': 'observe'}}
        with patch.object(self.a.command_hub, 'call', side_effect=RelayIdentityError('Fixture pin mismatch')):
            with self.assertRaises(RelayIdentityError):
                self.a.api('remote', payload)
        self.assertEqual({}, self.a.local_state()['pending'])
        self.assertIsNone(self.b.agent_hub.call('poll'))

    def test_loss_of_first_recovery_still_returns_original_operation_id(self):
        payload = {'peer': self.target, 'request': {'action': 'observe'}}
        with patch.object(self.a, 'recover', side_effect=OSError('Fixture relay interrupted')):
            original = self.a.api('remote', payload)
        self.assertEqual('unconfirmed', original['state'])
        self.assertEqual(original['id'], self.b.agent_hub.call('poll')['id'])
        self.assertEqual(original['id'], self.a.local_state()['pending'][self.target])
        self.assertEqual(1, self.a.command_hub.calls.count('submit'))

    def test_slow_receipt_query_does_not_delay_pausing_incoming(self):
        self.a.api('availability', {'enabled': True})
        querying = threading.Event()
        release_reply = threading.Event()
        transport = self.a.command_hub.call

        def delay_reply(operation, body):
            if operation == 'recover':
                querying.set()
                if not release_reply.wait(2):
                    raise OSError('Fixture reply timed out')
            return transport(operation, body)

        payload = {'peer': self.target, 'request': {'action': 'observe'}}
        with patch.object(self.a.command_hub, 'call', side_effect=delay_reply):
            with ThreadPoolExecutor(max_workers=2) as tasks:
                submission = tasks.submit(self.a.api, 'remote', payload)
                try:
                    self.assertTrue(querying.wait(1))
                    pause = tasks.submit(self.a.api, 'availability', {'enabled': False})
                    self.assertFalse(pause.result(timeout=1)['incoming'])
                    self.assertFalse(self.a.agent.running())
                    self.assertFalse(submission.done())
                finally:
                    release_reply.set()
                self.assertEqual('unconfirmed', submission.result(timeout=1)['state'])

    def test_local_pending_survives_client_restart(self):
        self.a.command_hub.drop_submit = True
        value = self.a.api('remote', {'peer': self.target, 'request': {'action': 'observe'}})
        class ExistingAgent(FakeAgent):
            def __init__(self, home):
                self.workspace = Path(home) / 'worker'
                self.enabled = False
        restarted = DesktopClient(self.a.home, self.endpoint, LocalTransport, ExistingAgent)
        self.assertEqual(value['id'], restarted.pending[self.target])

    def test_mailbox_recovers_abandoned_snapshot_without_running_it_again(self):
        payload = {'peer': self.target, 'request': {'action': 'observe'}}
        submitted = self.a.api('remote', payload)
        command = self.b.agent_hub.call('poll')
        delivery = self.b.agent.workspace / 'delivery.json'
        outbox = self.b.agent.workspace / 'hub-outbox.json'
        atomic_json(delivery, command)

        self.b._sync_commands()
        self.assertTrue(delivery.exists())
        self.assertFalse(outbox.exists())
        past = time.time() - 46
        os.utime(delivery, (past, past))
        with patch.object(self.b.agent_hub, 'call', side_effect=OSError('Fixture response lost')):
            with self.assertRaises(OSError):
                self.b._sync_commands()
        self.assertFalse(delivery.exists())
        self.assertTrue(outbox.exists())
        self.b._sync_commands()
        self.assertFalse(outbox.exists())
        receipt = self.a.api('recover', {'id': submitted['id']})['result']
        self.assertEqual('SCREEN_CAPTURE_TIMED_OUT', receipt['error'])
        self.assertFalse(receipt['input_attempted'])

        submitted = self.a.api('remote', payload)
        self.assertEqual(submitted['id'], self.b.agent_hub.call('poll')['id'])

    def test_restart_clears_delivery_when_saved_receipt_was_left_in_outbox(self):
        payload = {'peer': self.target, 'request': {'action': 'text', 'text': 'fixture', 'frame_id': 'f' * 32}}
        submitted = self.a.api('remote', payload)
        self.b.agent = MailboxAgent(self.b.home)
        workspace = self.b.agent.workspace
        delivery = workspace / 'delivery.json'
        outbox = workspace / 'hub-outbox.json'
        unlink = Path.unlink

        def interrupt_delivery_unlink(path, *args, **kwargs):
            if path == delivery:
                raise OSError('Fixture interruption between outbox write and delivery unlink')
            return unlink(path, *args, **kwargs)

        with patch.object(Path, 'unlink', autospec=True, side_effect=interrupt_delivery_unlink):
            with self.assertRaises(OSError):
                self.b._sync_commands()
        self.assertTrue(delivery.exists())
        self.assertTrue(outbox.exists())

        self.hub.close()
        self.hub = PeerHub(Path(self.folder.name) / 'hub.sqlite')
        LocalTransport.hub = self.hub
        restarted = DesktopClient(self.b.home, self.endpoint, LocalTransport, MailboxAgent)
        restarted._sync_commands()
        receipt = self.a.api('recover', {'id': submitted['id']})['result']
        self.assertEqual('LOCAL_CONTROL_NOT_ENABLED', receipt['error'])
        self.assertFalse(receipt['input_attempted'])
        self.assertFalse(outbox.exists())
        self.assertFalse(delivery.exists())
        next_operation = self.a.api('remote', payload)
        restarted._sync_commands()
        self.assertFalse(self.a.api('recover', {'id': next_operation['id']})['result']['input_attempted'])
        self.assertEqual([], list(workspace.glob('*.request.json')))

    def test_sleep_during_poll_does_not_publish_an_expired_input(self):
        payload = {'peer': self.target, 'request': {'action': 'text', 'text': 'fixture', 'frame_id': 'f' * 32}}
        submitted = self.a.api('remote', payload)
        self.b.agent = MailboxAgent(self.b.home)
        transport = self.b.agent_hub.call
        wall = 1000

        def delayed_poll(operation, body=None):
            nonlocal wall
            value = transport(operation, body)
            if operation == 'poll':
                wall += 60
            return value

        with patch('lanpilot.peer_client.time.monotonic', return_value=500), \
             patch('lanpilot.peer_client.time.time', side_effect=lambda: wall), \
             patch.object(self.b.agent, 'running', return_value=True), \
             patch.object(self.b.agent_hub, 'call', side_effect=delayed_poll):
            self.b._sync_commands()
        self.assertEqual([], list(self.b.agent.workspace.glob('*.request.json')))
        receipt = self.a.api('recover', {'id': submitted['id']})['result']
        self.assertEqual('REQUEST_EXPIRED_IN_TRANSIT', receipt['error'])
        self.assertFalse(receipt['input_attempted'])

    def test_disabling_availability_stops_local_agent(self):
        self.assertTrue(self.b.agent.enabled)
        self.b.api('availability', {'enabled': False})
        self.assertFalse(self.b.agent.enabled)
        with self.assertRaises(PermissionError):
            self.hub.dispatch('submit', self.a.config['credential'],
                              {'peer': self.target, 'id': 'a' * 32, 'request': {'action': 'observe'}})

    def test_restart_restores_only_unpaused_long_term_access(self):
        def restart():
            self.b.close()
            self.b = DesktopClient(self.b.home, self.endpoint, LocalTransport, FakeAgent)
            with patch.object(self.b.stop_event, 'wait', side_effect=lambda _: self.b.stop_event.set()):
                self.b._pump()
            return self.a.agent_hub.call('state')['peers'][0]['can_control']

        self.assertFalse(restart())
        self.assertFalse(self.b.agent.enabled)
        self.b.api('availability', {'enabled': True})
        approval = self.a.api('access', {'peer': self.target})['approval']
        self.b.api('approve', {'approval': approval, 'allow': True, 'persistent': True})
        self.assertTrue(restart())
        self.assertTrue(self.b.agent.enabled)
        self.b.api('availability', {'enabled': False})
        self.assertFalse(restart())
        self.assertFalse(self.b.agent.enabled)

    def test_worker_permission_failure_does_not_show_relay_offline_at_startup(self):
        approval = self.a.api('access', {'peer': self.target})['approval']
        self.b.api('approve', {'approval': approval, 'allow': True, 'persistent': True})
        self.b.close()
        restarted = DesktopClient(self.b.home, self.endpoint, LocalTransport, FakeAgent)
        with patch.object(restarted.agent, 'start', side_effect=PermissionError('Fixture permission unavailable')), \
             patch.object(restarted.stop_event, 'wait', side_effect=lambda _: restarted.stop_event.set()):
            restarted._pump()
        self.assertTrue(restarted.local_state()['connected'])
        self.assertFalse(restarted.local_state()['incoming'])
        self.assertIn('Fixture permission unavailable', restarted.local_state()['agent_error'])
        self.assertFalse(self.a.agent_hub.call('state')['peers'][0]['can_control'])

    def test_offline_pause_stops_locally_and_syncs_when_relay_returns(self):
        approval = self.a.api('access', {'peer': self.target})['approval']
        self.b.api('approve', {'approval': approval, 'allow': True, 'persistent': True})
        self.b.state = self.b.agent_hub.call('state')
        self.assertTrue(self.b.local_state()['incoming'])
        before = self.a.agent_hub.call('state')['peers'][0]
        self.assertTrue(before['online'])
        self.assertTrue(before['accepting_connections'])
        with patch.object(self.b.ui_hub, 'call', side_effect=OSError('Fixture Wi-Fi interruption')):
            result = self.b.api('availability', {'enabled': False})
        self.assertFalse(result['incoming'])
        self.assertFalse(self.b.local_state()['incoming'])
        self.assertFalse(self.b.agent.enabled)
        self.assertFalse(json.loads(self.b.path.read_text())['resume_trusted_access'])
        self.assertTrue(self.b.local_state()['pause_pending'])

        # The relay still has the earlier availability until the target reconnects.
        payload = {'peer': self.target, 'request': {'action': 'text', 'text': 'fixture', 'frame_id': 'f' * 32}}
        queued = self.a.api('remote', payload)
        self.assertEqual('unconfirmed', queued['state'])
        self.b._reconcile_incoming()
        self.b._sync_commands()
        recovered = self.a.api('recover', {'id': queued['id']})
        self.assertEqual('complete', recovered['state'])
        self.assertFalse(recovered['result']['input_attempted'])
        self.assertEqual('EXPIRED_OR_ACCESS_REVOKED_BEFORE_DELIVERY', recovered['result']['error'])
        self.assertFalse(self.b.local_state()['pause_pending'])
        after = self.a.agent_hub.call('state')['peers'][0]
        self.assertTrue(after['online'])
        self.assertFalse(after['accepting_connections'])
        self.assertFalse(after['can_control'])
        self.assertTrue(self.b.agent_hub.call('state')['peers'][0]['trusted_incoming'])

    def test_stopped_worker_does_not_keep_advertising_incoming_control(self):
        self.b.agent.stop()
        self.b._reconcile_incoming()
        state = self.b.local_state()
        self.assertFalse(state['incoming'])
        self.assertIn('接收进程已停止', state['agent_error'])
        self.assertFalse(self.a.agent_hub.call('state')['peers'][0]['can_control'])

    def test_failed_resume_does_not_overwrite_explicit_pause(self):
        approval = self.a.api('access', {'peer': self.target})['approval']
        self.b.api('approve', {'approval': approval, 'allow': True, 'persistent': True})
        self.b.api('availability', {'enabled': False})
        delivered = self.b.ui_hub.call

        def drop_reply(operation, body):
            delivered(operation, body)
            raise OSError('Fixture response lost after the relay accepted it')

        with patch.object(self.b.ui_hub, 'call', side_effect=drop_reply):
            with self.assertRaises(OSError):
                self.b.api('availability', {'enabled': True})
        self.assertFalse(self.b.local_state()['incoming'])
        self.assertFalse(self.b.agent.running())
        self.assertFalse(json.loads(self.b.path.read_text())['resume_trusted_access'])
        self.b._reconcile_incoming()
        self.assertFalse(self.a.agent_hub.call('state')['peers'][0]['can_control'])

    def test_aged_relay_state_is_reported_as_stale_during_socket_timeout(self):
        with patch.object(self.b.stop_event, 'wait', side_effect=lambda _: self.b.stop_event.set()):
            self.b._pump()
        self.assertTrue(self.b.local_state()['connected'])
        with patch('lanpilot.peer_client.time.monotonic', return_value=self.b.state_seen_at + 9):
            self.assertFalse(self.b.local_state()['connected'])

    def test_short_outage_invalidates_peer_status_until_relay_replies(self):
        observed = []
        polls = 0
        transport = self.a.agent_hub.call

        def drop_one_poll(operation, body=None):
            nonlocal polls
            if operation == 'poll':
                polls += 1
                if polls == 2:
                    raise OSError('Fixture interruption')
            return transport(operation, body)

        def capture_state(_):
            snapshot = self.a.local_state()
            observed.append((snapshot['connected'], snapshot['peers'][0]['id']))
            if len(observed) == 3:
                self.a.stop_event.set()

        with patch.object(self.a.agent_hub, 'call', side_effect=drop_one_poll), \
             patch.object(self.a.stop_event, 'wait', side_effect=capture_state):
            self.a._pump()
        self.assertEqual([(True, self.target), (False, self.target), (True, self.target)], observed)

    def test_resume_invalidates_cached_state_when_uptime_clock_stays_still(self):
        wall = 1000
        observed = []

        def suspend_between_polls(_):
            nonlocal wall
            observed.append(self.a.local_state()['connected'])
            if len(observed) == 1:
                wall += 60
                observed.append(self.a.local_state()['connected'])
            else:
                self.a.stop_event.set()

        with patch('lanpilot.peer_client.time.monotonic', return_value=500), \
             patch('lanpilot.peer_client.time.time', side_effect=lambda: wall), \
             patch.object(self.a.stop_event, 'wait', side_effect=suspend_between_polls):
            self.a._pump()
        self.assertEqual([True, False, True], observed)
        self.assertEqual(3, self.a.agent_hub.calls.count('state'))

    def test_reply_started_before_sleep_does_not_make_devices_current(self):
        wall = 1000
        snapshots = []
        transport = self.a.agent_hub.call
        state_calls = 0

        def suspend_during_reply(operation, body=None):
            nonlocal wall, state_calls
            result = transport(operation, body)
            if operation == 'state':
                state_calls += 1
                if state_calls == 2:
                    wall += 60
            return result

        def capture_state(_):
            snapshots.append(self.a.local_state()['connected'])
            if len(snapshots) == 2:
                self.a.stop_event.set()

        with patch('lanpilot.peer_client.time.monotonic', return_value=500), \
             patch('lanpilot.peer_client.time.time', side_effect=lambda: wall), \
             patch.object(self.a.agent_hub, 'call', side_effect=suspend_during_reply), \
             patch.object(self.a.stop_event, 'wait', side_effect=capture_state):
            self.a._pump()
        self.assertEqual([False, True], snapshots)

    def test_single_instance_lock(self):
        one = InstanceLock(Path(self.folder.name) / 'locks')
        two = InstanceLock(Path(self.folder.name) / 'locks')
        try:
            self.assertTrue(one.acquire())
            self.assertFalse(two.acquire())
            one.close()
            self.assertTrue(two.acquire())
        finally:
            one.close()
            two.close()


class LinuxClientTests(unittest.TestCase):
    def make_client(self, home):
        endpoint = {'url': 'https://hub.example:443', 'pin': 'a' * 64}
        return DesktopClient(home, endpoint, LocalTransport)

    def test_linux_profile_uses_absolute_xdg_config_or_home_fallback(self):
        with patch('lanpilot.peer_client.sys.platform', 'linux'), patch('lanpilot.peer_client.Path.home', return_value=Path('/home/test')):
            for configured, expected in (('/test/config', '/test/config'), ('relative', '/home/test/.config'), ('', '/home/test/.config')):
                with patch.dict(os.environ, {'XDG_CONFIG_HOME': configured}):
                    self.assertEqual(Path(expected) / 'linko-remote', default_home())

    def test_linux_can_pair_and_start_existing_desktop_modes_without_a_native_worker(self):
        with tempfile.TemporaryDirectory() as directory, closing(PeerHub(Path(directory) / 'hub.sqlite')) as hub:
            root = Path(directory)
            token = secrets.token_urlsafe(32)
            target = hub.dispatch('register', token, {'platform': 'windows', 'name': 'Fixture PC', 'live_version': 1})['id']
            hub.dispatch('availability', token, {'enabled': True})
            code = hub.dispatch('code', token, {})['code']
            with patch.object(LocalTransport, 'hub', hub), patch('lanpilot.peer_client.sys.platform', 'linux'), patch('lanpilot.peer_client.subprocess.Popen') as popen:
                linux = self.make_client(root / 'profile')
                with patch.object(linux.stop_event, 'wait', side_effect=lambda _: linux.stop_event.set()):
                    linux._pump()
                linux.stop_event.clear()
                state = linux.local_state()
                self.assertEqual('linux', state['self']['platform'])
                self.assertFalse(state['self']['accepts_desktop'])
                self.assertTrue(state['connected'])
                self.assertFalse(state['supports_incoming'])
                self.assertFalse(state['supports_updates'])
                self.assertNotIn('poll', linux.agent_hub.calls)
                for enable in (linux.api, linux.ui_hub.call):
                    with self.assertRaisesRegex(PermissionError, '暂未支持接收'):
                        enable('availability', {'enabled': True})
                with self.assertRaisesRegex(PermissionError, '暂未支持接收'):
                    linux.agent.start()
                with self.assertRaisesRegex(ValueError, '通过安装包更新'):
                    linux.api('update-check', {})

                approval = linux.api('connect', {'code': code})['approval']
                hub.dispatch('approve', token, {'approval': approval, 'allow': True})
                peer = hub.dispatch('state', token, {})['peers'][0]
                self.assertFalse(peer['accepts_desktop'])
                self.assertFalse(peer['live'])
                guarded = linux.api('remote', {'peer': target, 'request': {'action': 'observe'}})
                self.assertEqual(guarded['id'], hub.dispatch('poll', token, {})['id'])
                receipt = {'ok': True, 'input_attempted': False, 'request_id': guarded['id']}
                hub.dispatch('result', token, {'id': guarded['id'], 'result': receipt})
                self.assertEqual(receipt, linux.api('recover', {'id': guarded['id']})['result'])
                with patch('lanpilot.peer_client.LiveView.start'):
                    live = linux.api('live-open', {'peer': target})
                self.assertEqual(live, hub.dispatch('live-agent-state', token, {})['sessions'][0])
                self.assertEqual({'closed': True}, linux.api('live-close', live))
                popen.assert_not_called()

    def test_ssh_alias_port_can_be_inherited_or_explicit(self):
        with tempfile.TemporaryDirectory() as directory, patch('lanpilot.peer_client.sys.platform', 'linux'):
            client = self.make_client(directory)
            config = Path(directory) / 'ssh_config'
            config.write_text('Host work-alias\n  Port 2227\n')
            with patch.object(client, 'open_linux_ssh') as launch:
                for port, expected in [('', 2227), ('22', 22), (2244, 2244)]:
                    profile = client.save_ssh({'name': 'Office', 'host': 'work-alias', 'port': port})
                    client.open_ssh(profile['id'])
                    args = launch.call_args.args[0]
                    result = subprocess.run([args[0], '-G', '-F', str(config), *args[1:]],
                                            capture_output=True, text=True, check=True)
                    self.assertIn(f'port {expected}', result.stdout.splitlines())

    def test_linux_ssh_launches_open_ssh_in_desktop_terminal_with_separate_arguments(self):
        tools = {'ssh': '/usr/bin/ssh', 'konsole': '/usr/bin/konsole'}
        with tempfile.TemporaryDirectory() as directory, patch('lanpilot.peer_client.sys.platform', 'linux'):
            linux = self.make_client(directory)
            ssh = linux.save_ssh({'name': 'Test host', 'port': 2222, 'user': 'alice', 'host': 'fixture.example'})
            with patch.dict(os.environ, {'WAYLAND_DISPLAY': 'wayland-0'}), patch('lanpilot.peer_client.shutil.which', side_effect=tools.get), patch('lanpilot.peer_client.subprocess.Popen') as popen:
                self.assertEqual({'opened': True}, linux.open_ssh(ssh['id']))
                self.assertEqual(['/usr/bin/konsole', '-e', '/usr/bin/ssh', '-p', '2222', '-l', 'alice', '--', 'fixture.example'], popen.call_args.args[0])
                self.assertFalse(popen.call_args.kwargs.get('shell', False))
            with patch.dict(os.environ, {'WAYLAND_DISPLAY': '', 'DISPLAY': ''}), patch('lanpilot.peer_client.shutil.which', side_effect=tools.get):
                with self.assertRaisesRegex(ValueError, 'ssh -p 2222 -l alice -- fixture.example'):
                    linux.open_ssh(ssh['id'])


class LocalHTTPTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        class FakeClient:
            def api(_, operation, body):
                self.calls.append(operation)
                return {'called': operation}
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), LocalHandler)
        self.server.authority = '127.0.0.1:' + str(self.server.server_port)
        self.server.nonce = secrets.token_urlsafe(32)
        self.server.client = FakeClient()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, method='POST', path='/api/state', headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        connection.request(method, path, '{}' if method == 'POST' else None, headers or {})
        response = connection.getresponse()
        value = (response.status, response.read(), dict(response.getheaders()))
        connection.close()
        return value

    def test_api_rejects_cross_site_and_dns_rebinding(self):
        self.assertEqual(403, self.request()[0])
        authorization = {'X-Linko-Session': self.server.nonce}
        self.assertEqual(403, self.request(headers=dict(authorization, Origin='https://malicious.example'))[0])
        self.assertEqual(403, self.request(headers=dict(authorization, Host='malicious.example'))[0])
        self.assertEqual([], self.calls)
        self.assertEqual(200, self.request(headers=authorization)[0])
        self.assertEqual(['state'], self.calls)

    def test_only_fixed_shared_assets_are_public_and_have_csp(self):
        for path in ('/', '/app.js', '/style.css'):
            status, raw, headers = self.request('GET', path)
            self.assertEqual(200, status)
            self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
            self.assertNotIn(self.server.nonce.encode(), raw)
        for path in ('/../client.json', '/client.json', '/api/state', '/style.css?other'):
            self.assertEqual(404, self.request('GET', path)[0])


class TLSReconnectTests(unittest.TestCase):
    def test_idle_connection_is_retired_before_any_new_request(self):
        events = []
        class Response:
            status = 200
            def read(self, maximum): return b'{"ok":true,"value":{}}'
        class Connection:
            def __init__(self, *args): self.sock = None; events.append('new')
            def connect(self): self.sock = object()
            def request(self, *args): events.append('send')
            def getresponse(self): return Response()
            def close(self): events.append('close')
        client = PinnedHub({'url': 'https://hub.example', 'pin': 'a'*64}, secrets.token_urlsafe(32))
        with patch('lanpilot.peer_client.PinnedConnection', Connection), patch('lanpilot.peer_client.time.monotonic', side_effect=[100,121,121]):
            client.call('state')
            client.call('code')
        self.assertEqual(['new','send','close','new','send'],events)

    def test_retries_handshake_before_sending_a_mutation(self):
        client = PinnedHub({'url': 'https://hub.example', 'pin': 'a'*64}, secrets.token_urlsafe(32))
        with patch('lanpilot.peer_client.PinnedConnection') as factory:
            failed, working = factory.return_value, factory.return_value
            failed.connect.side_effect = [OSError('TLS EOF'), None]
            working.getresponse().status = 200
            working.getresponse().read.return_value = b'{"ok":true,"value":{"id":"only-once"}}'
            self.assertEqual({'id': 'only-once'}, client.call('submit', {}))
            self.assertEqual(2, working.connect.call_count)
            working.request.assert_called_once()

    def test_snapshot_recovery_reconnects_over_loopback_without_replaying_either_peer(self):
        class JitterHandler(Handler):
            def answer(self, status, value):
                if self.path == '/v2/recover' and self.server.drop_next_recovery:
                    self.server.drop_next_recovery = False
                    self.close_connection = True
                    return
                return super().answer(status, value)

        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            key, cert = folder / 'fixture.key', folder / 'fixture.crt'
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                            '-subj', '/CN=localhost', '-keyout', str(key), '-out', str(cert)],
                           check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            pin = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest()
            with PeerGateway(folder, cert, key, 0) as gateway:
                gateway.RequestHandlerClass = JitterHandler
                gateway.drop_next_recovery = False
                thread = threading.Thread(target=gateway.serve_forever, daemon=True)
                thread.start()
                endpoint = {'url': f'https://127.0.0.1:{gateway.server_port}', 'pin': pin}
                caller, first, second = [PinnedHub(endpoint, secrets.token_urlsafe(32)) for _ in range(3)]
                try:
                    caller.call('register', {'name': 'Fixture Mac', 'platform': 'macos'})
                    known = []
                    for index, target in enumerate((first, second)):
                        peer = target.call('register', {'name': f'Fixture PC {index}', 'platform': 'windows'})['id']
                        known.append(peer)
                        target.call('availability', {'enabled': True})
                        code = target.call('code')['code']
                        approval = caller.call('connect', {'code': code})['approval']
                        target.call('approve', {'approval': approval, 'allow': True})

                    with patch.object(gateway.hub, 'dispatch', wraps=gateway.hub.dispatch) as dispatch:
                        identifiers = [uuid.uuid4().hex for _ in known]
                        for peer, identifier in zip(known, identifiers):
                            caller.call('submit', {'peer': peer, 'id': identifier, 'request': {'action': 'observe'}})
                            socket_before_recover = caller.connection.sock
                            self.assertIsNotNone(socket_before_recover)
                            self.assertEqual('unconfirmed', caller.call('recover', {'id': identifier})['state'])
                            self.assertIs(socket_before_recover, caller.connection.sock)

                        gateway.drop_next_recovery = True
                        self.assertEqual('unconfirmed', caller.call('recover', {'id': identifiers[0]})['state'])
                        self.assertFalse(gateway.drop_next_recovery)
                        self.assertIsNot(socket_before_recover, caller.connection.sock)

                        for target, identifier in zip((first, second), identifiers):
                            self.assertEqual(identifier, target.call('poll')['id'])
                            receipt = {'request_id': identifier, 'ok': True, 'input_attempted': False}
                            target.call('result', {'id': identifier, 'result': receipt})
                            self.assertEqual(receipt, caller.call('recover', {'id': identifier})['result'])
                            self.assertIsNone(target.call('poll'))
                        self.assertEqual(2, sum(call.args[0] == 'submit' for call in dispatch.call_args_list))
                finally:
                    for client in (caller, first, second):
                        if client.connection:
                            client.connection.close()
                    gateway.shutdown()
                    thread.join(timeout=2)
                    gateway.hub.close()

    def test_changed_certificate_on_automatic_reconnect_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            certificates = []
            for name in ('first', 'second'):
                key, cert = folder / (name + '.key'), folder / (name + '.crt')
                subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                                '-subj', '/CN=localhost', '-keyout', str(key), '-out', str(cert)],
                               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                certificates.append((key, cert))
            calls = []
            class Handler(http.server.BaseHTTPRequestHandler):
                def log_message(self, *_): pass
                def do_POST(self):
                    self.rfile.read(int(self.headers['Content-Length']))
                    calls.append(True)
                    raw = b'{"ok":true,"value":{}}'
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(raw)))
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    self.wfile.write(raw)
            server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(str(certificates[0][1]), str(certificates[0][0]))
            server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            pin = hashlib.sha256(ssl.PEM_cert_to_DER_cert(certificates[0][1].read_text())).hexdigest()
            client = PinnedHub({'url': 'https://127.0.0.1:' + str(server.server_port), 'pin': pin}, secrets.token_urlsafe(32))
            try:
                self.assertEqual({}, client.call('state'))
                context.load_cert_chain(str(certificates[1][1]), str(certificates[1][0]))
                with self.assertRaisesRegex(PermissionError, '证书'):
                    client.call('state')
                self.assertEqual(1, len(calls), 'No credential-bearing request may reach the changed certificate')
            finally:
                if client.connection:
                    client.connection.close()
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == '__main__':
    unittest.main()
