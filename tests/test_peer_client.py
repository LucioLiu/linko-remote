import hashlib
import http.client
import http.server
import json
from pathlib import Path
import secrets
import ssl
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from lanpilot.peer_client import DesktopClient, HubRejection, LocalHandler, PinnedHub, InstanceLock
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

    def permissions(self):
        return {'screen': True, 'input': True}


class ClientTests(unittest.TestCase):
    def setUp(self):
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
        self.a.ui_hub.drop_submit = True
        value = self.a.api('remote', {'peer': self.target, 'request': {'action': 'observe'}})
        self.assertEqual('unconfirmed', value['state'])
        with self.assertRaisesRegex(ValueError, '未确认'):
            self.a.api('remote', {'peer': self.target, 'request': {'action': 'observe'}})
        self.assertEqual(1, self.a.ui_hub.calls.count('submit'))
        command = self.b.agent_hub.call('poll')
        receipt = {'request_id': command['id'], 'ok': True, 'input_attempted': False,
                   'result': {'frame_id': 'a' * 32}}
        self.b.agent_hub.call('result', {'id': command['id'], 'result': receipt})
        recovered = self.a.api('recover', {'id': command['id']})
        self.assertEqual('complete', recovered['state'])
        self.assertEqual({}, self.a.pending)

    def test_rejected_submission_clears_pending_and_preserves_rejection(self):
        self.a.api('disconnect', {'peer': self.target})
        payload = {'peer': self.target, 'request': {'action': 'observe'}}
        with self.assertRaisesRegex(HubRejection, '尚未取得对方授权'):
            self.a.api('remote', payload)
        self.assertEqual({}, self.a.pending)
        self.assertEqual({}, json.loads(self.a.pending_path.read_text()))
        self.assertNotIn('recover', self.a.ui_hub.calls)

        approval = self.a.api('access', {'peer': self.target})['approval']
        self.b.api('approve', {'approval': approval, 'allow': True})
        submitted = self.a.api('remote', payload)
        self.assertEqual(submitted['id'], self.b.agent_hub.call('poll')['id'])

    def test_local_pending_survives_client_restart(self):
        self.a.ui_hub.drop_submit = True
        value = self.a.api('remote', {'peer': self.target, 'request': {'action': 'observe'}})
        class ExistingAgent(FakeAgent):
            def __init__(self, home):
                self.workspace = Path(home) / 'worker'
                self.enabled = False
        restarted = DesktopClient(self.a.home, self.endpoint, LocalTransport, ExistingAgent)
        self.assertEqual(value['id'], restarted.pending[self.target])

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
