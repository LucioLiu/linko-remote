import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.client
import http.server
import io
import json
import os
from pathlib import Path
import secrets
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from urllib.parse import quote
from unittest.mock import patch
import zipfile

from lanpilot.file_transfer import FILE_CHUNK, FILE_IDLE_TTL, FILE_MAX, FILE_VERSION
from lanpilot.peer_client import DesktopClient, LocalHandler
from lanpilot.peer_gateway import PeerGateway


@unittest.skipUnless(shutil.which('openssl'), 'openssl is required for the TLS fixture')
class FileTransferTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        cert, key = self.root / 'cert.pem', self.root / 'key.pem'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-subj', '/CN=localhost', '-keyout', str(key), '-out', str(cert)],
                       check=True, capture_output=True)
        self.gateway = PeerGateway(self.root, cert, key, 0)
        self.addCleanup(self.gateway.hub.close)
        self.serve(self.gateway)
        self.endpoint = {'url': f'https://127.0.0.1:{self.gateway.server_port}',
                         'pin': hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest()}
        self.a = self.desktop('macos')
        self.b = self.desktop('windows')
        self.a_client, self.b_client = self.a.client, self.b.client
        self.a_id, self.b_id = self.a_client.state['self']['id'], self.b_client.state['self']['id']
        sender = self.a_client.agent_hub
        receiver = self.b_client.agent_hub
        receiver.call('availability', {'enabled': True})
        approval = sender.call('connect', {'code': receiver.call('code')['code']})['approval']
        receiver.call('approve', {'approval': approval, 'allow': True})
        receiver.call('availability', {'enabled': False})
        sender.call('disconnect', {'peer': self.b_id})
        self.sync(self.a)
        self.sync(self.b)

    def serve(self, server):
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .025}, daemon=True)
        thread.start()
        def stop():
            server.shutdown()
            server.server_close()
            thread.join()
        self.addCleanup(stop)

    def desktop(self, platform, version=FILE_VERSION):
        with patch('lanpilot.peer_client.desktop_platform', return_value=platform):
            client = DesktopClient(self.root / platform, self.endpoint)
        hub = client.agent_hub
        hub.call('register', {'name': platform, 'platform': platform, 'file_version': version})
        client.state = hub.call('state', {'file_version': version})
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), LocalHandler)
        server.daemon_threads = True
        server.authority = '127.0.0.1:' + str(server.server_port)
        server.nonce = secrets.token_urlsafe(32)
        server.client = client
        def close():
            client.files.close()
            if hub.connection:
                hub.connection.close()
        self.addCleanup(close)
        self.serve(server)
        return server

    @staticmethod
    def sync(server):
        client = server.client
        client.state = client.agent_hub.call('state', {'file_version': FILE_VERSION})
        return client.local_state()

    @staticmethod
    def request(server, method, path, value=None, *, valid_session=True):
        conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=7)
        try:
            headers = {'X-Linko-Session': server.nonce} if valid_session else {}
            conn.request(method, path, json.dumps(value or {}) if method == 'POST' else None, headers)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def api(self, server, op, value=None):
        status, _, raw = self.request(server, 'POST', '/api/' + op, value)
        answer = json.loads(raw)
        if status != 200:
            raise ValueError(answer['error'])
        return answer['value']

    def wait(self, action, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = action()
            if result:
                return result
            time.sleep(.025)
        self.fail('timed out before expected file state')

    def phase(self, server, identifier, expected):
        return self.wait(lambda: self.api(server, 'file-status', {'id': identifier})['phase'] == expected)

    def offer(self, sender, name, size):
        peer_id = self.b_id if sender is self.a else self.a_id
        offer = self.api(sender, 'file-offer', {'peer': peer_id, 'name': name, 'size': size})
        return {'id': offer['id']}

    def push(self, sender, ident, offset, chunk):
        body = dict(ident, offset=offset, data=base64.b64encode(chunk).decode())
        self.wait(lambda: self.api(sender, 'file-push', body)['accepted'])

    @staticmethod
    def zip_bytes():
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_STORED) as output:
            output.writestr('payload.bin', os.urandom(13 * 1024 * 1024))
        return archive.getvalue()

    def download(self, server, ident, expected_name):
        self.wait(lambda: any(item.get('downloadable') and item['id'] == ident['id']
                              for item in self.sync(server)['files']))
        path = self.api(server, 'file-download', ident)['url']
        status, headers, data = self.request(server, 'GET', path)
        self.assertEqual(200, status)
        self.assertEqual('application/octet-stream', headers['Content-Type'])
        self.assertEqual("attachment; filename*=UTF-8''" + quote(expected_name), headers['Content-Disposition'])
        return data

    def test_bidirectional_13_mib_zip_over_tls_with_separate_consent(self):
        self.assertFalse(self.a_client.state['peers'][0]['can_control'])
        self.assertFalse(self.b_client.state['peers'][0]['can_control'])
        a_data, b_data = self.zip_bytes(), self.zip_bytes()
        a = self.offer(self.a, '发往 Windows.zip', len(a_data))
        b = self.offer(self.b, '发往 Mac.zip', len(b_data))
        self.assertEqual(403, self.request(self.b, 'POST', '/api/file-accept', a, valid_session=False)[0])
        with self.assertRaisesRegex(ValueError, '尚未同意'):
            self.push(self.a, a, 0, a_data[:FILE_CHUNK])
        with self.assertRaisesRegex(ValueError, '尚未接收'):
            self.api(self.b, 'file-download', a)
        self.assertFalse(self.b_client.files.spool.exists())
        self.api(self.b, 'file-accept', a)
        self.api(self.a, 'file-accept', b)
        self.phase(self.a, a['id'], 'transferring')
        self.phase(self.b, b['id'], 'transferring')

        def transmit(sender, receiver, ident, data, name):
            for offset in range(0, len(data), FILE_CHUNK):
                self.push(sender, ident, offset, data[offset:offset+FILE_CHUNK])
            self.api(sender, 'file-seal', ident)
            self.phase(sender, ident['id'], 'complete')
            self.assertEqual(data, self.download(receiver, ident, name))

        with ThreadPoolExecutor(max_workers=2) as pool:
            forwards = pool.submit(transmit, self.a, self.b, a, a_data, '发往 Windows.zip')
            transmit(self.b, self.a, b, b_data, '发往 Mac.zip')
            forwards.result()

    def test_tls_disconnect_during_receive_clears_chunk_and_partial_file(self):
        ident = self.offer(self.a, 'interrupted.zip', 2 * FILE_CHUNK)
        self.api(self.b, 'file-accept', ident)
        self.phase(self.a, ident['id'], 'transferring')
        self.push(self.a, ident, 0, b'a' * FILE_CHUNK)
        self.wait(lambda: any(path.stat().st_size == FILE_CHUNK
                              for path in self.b_client.files.spool.glob('*.part')))
        transfer = self.gateway.hub.files.items[ident['id']]
        # Drop the next pull at the server after it receives the pinned TLS
        # request. The receiver gets EOF, then reconnects to cancel the transfer.
        original = self.gateway.hub.dispatch
        dropped = threading.Event()

        def dispatch(operation, credential, body, source='local'):
            if operation == 'file-pull' and body.get('id') == ident['id'] and not dropped.is_set():
                dropped.set()
                raise ConnectionAbortedError('fixture closed TLS connection')
            return original(operation, credential, body, source)

        with patch.object(self.gateway.hub, 'dispatch', dispatch):
            self.assertTrue(dropped.wait(3))
            self.phase(self.a, ident['id'], 'failed')
        self.wait(lambda: not any(self.b_client.files.spool.glob('*.part')))
        self.assertEqual('', transfer.chunk)
        with self.assertRaisesRegex(ValueError, '尚未接收'):
            self.api(self.b, 'file-download', ident)

    def test_decline_cancel_expiry_and_wrong_checksum(self):
        ident = self.offer(self.a, 'declined.txt', 0)
        self.api(self.b, 'file-cancel', ident)
        self.phase(self.a, ident['id'], 'declined')
        ident = self.offer(self.a, 'cancel.txt', 2 * FILE_CHUNK)
        self.api(self.b, 'file-accept', ident)
        self.phase(self.a, ident['id'], 'transferring')
        self.push(self.a, ident, 0, b'b' * FILE_CHUNK)
        self.api(self.a, 'file-cancel', ident)
        self.phase(self.b, ident['id'], 'cancelled')
        self.wait(lambda: not any(self.b_client.files.spool.glob('*.part')))

        ident = self.offer(self.a, 'timeout.txt', FILE_CHUNK)
        self.api(self.b, 'file-accept', ident)
        self.phase(self.a, ident['id'], 'transferring')
        future = self.gateway.hub.clock() + FILE_IDLE_TTL + 1
        with patch.object(self.gateway.hub, 'clock', return_value=future):
            self.phase(self.b, ident['id'], 'failed')
        self.wait(lambda: not any(self.b_client.files.spool.glob('*.part')))

        self.sync(self.a)
        self.sync(self.b)
        ident = self.offer(self.a, 'checksum.txt', FILE_CHUNK)
        self.api(self.b, 'file-accept', ident)
        self.phase(self.a, ident['id'], 'transferring')
        self.push(self.a, ident, 0, b'c' * FILE_CHUNK)
        with self.assertRaisesRegex(PermissionError, '校验失败'):
            self.a_client.agent_hub.call('file-seal', dict(ident, sha256='0' * 64))
        self.phase(self.b, ident['id'], 'failed')
        self.wait(lambda: not any(self.b_client.files.spool.glob('*.part')))

    def test_receiver_rejects_corrupted_data_before_download(self):
        ident = self.offer(self.a, 'corrupt.zip', FILE_CHUNK)
        self.api(self.b, 'file-accept', ident)
        self.phase(self.a, ident['id'], 'transferring')
        original = self.gateway.hub.dispatch
        bad_data = base64.b64encode(b'x' * FILE_CHUNK).decode()

        def dispatch(operation, credential, body, source='local'):
            result = original(operation, credential, body, source)
            if operation == 'file-pull' and body.get('id') == ident['id'] and 'data' in result:
                return dict(result, data=bad_data)
            return result

        with patch.object(self.gateway.hub, 'dispatch', dispatch):
            self.push(self.a, ident, 0, b'y' * FILE_CHUNK)
            self.api(self.a, 'file-seal', ident)
            self.phase(self.b, ident['id'], 'failed')
        self.wait(lambda: not any(self.b_client.files.spool.glob('*.part')))
        def local_failure():
            return next((item for item in self.sync(self.b)['files']
                         if item['id'] == ident['id'] and item['error'] == '接收文件完整性校验失败'), None)
        failure = self.wait(local_failure)
        self.assertEqual(FILE_CHUNK, failure['received'])
        self.assertFalse(failure.get('downloadable'))
        with self.assertRaisesRegex(ValueError, '尚未接收'):
            self.api(self.b, 'file-download', ident)

    def test_old_version_unpaired_and_size_limit(self):
        with self.assertRaisesRegex(ValueError, '100 MiB'):
            self.offer(self.a, 'oversize.zip', FILE_MAX + 1)
        stranger = self.desktop('linux')
        with self.assertRaisesRegex(ValueError, '已配对'):
            self.api(stranger, 'file-offer', {'peer': self.a_id, 'name': 'file', 'size': 0})
        self.b_client.agent_hub.call('state')
        self.assertFalse(self.sync(self.a)['peers'][0]['file_transfer'])
        with self.assertRaisesRegex(ValueError, '不支持文件传输'):
            self.offer(self.a, 'old.txt', 0)
        self.a_client.state.pop('file_version')
        with self.assertRaisesRegex(ValueError, '连接服务尚未支持'):
            self.offer(self.a, 'old-relay.txt', 0)

    def test_empty_file_and_safe_download_name(self):
        ident = self.offer(self.a, '../NUL.txt', 0)
        self.api(self.b, 'file-accept', ident)
        self.phase(self.a, ident['id'], 'transferring')
        self.api(self.a, 'file-seal', ident)
        self.assertEqual(b'', self.download(self.b, ident, '_NUL.txt'))


if __name__ == '__main__':
    unittest.main()
