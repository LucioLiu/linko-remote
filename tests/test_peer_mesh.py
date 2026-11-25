import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import secrets
import ssl
import subprocess
import tempfile
import threading
import unittest

from lanpilot.peer_client import HubRejection, PinnedHub
from lanpilot.peer_gateway import Handler, PeerGateway


class LabSourceHandler(Handler):
    def do_POST(self):
        # Only the test harness models the source seen by the relay. The
        # request, TLS pin, bearer credential and authorization are real.
        credential = self.headers.get('Authorization', '').removeprefix('Bearer ')
        address = self.server.lab_sources.get(credential)
        if address:
            self.client_address = (address, self.client_address[1])
        super().do_POST()


class PeerMeshTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        folder = Path(directory.name)
        cert = folder / 'relay.crt'
        key = folder / 'relay.key'
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
                        '-subj', '/CN=localhost', '-keyout', str(key), '-out', str(cert)],
                       check=True, capture_output=True)
        pin = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text(encoding='ascii'))).hexdigest()
        gateway = PeerGateway(folder, cert, key, 0)
        self.addCleanup(gateway.hub.close)
        self.addCleanup(gateway.server_close)
        gateway.RequestHandlerClass = LabSourceHandler
        self.now = 1000
        gateway.hub.clock = lambda: self.now
        peers = [('Mac', 'macos', '192.0.2.15'), ('First Windows', 'windows', '198.51.100.20'),
                 ('Second Windows', 'windows', '203.0.113.31')]
        gateway.lab_sources = {}
        relay = threading.Thread(target=gateway.serve_forever, kwargs={'poll_interval': .05}, daemon=True)
        relay.start()
        self.addCleanup(relay.join, 2)
        self.addCleanup(gateway.shutdown)
        endpoint = {'url': f'https://127.0.0.1:{gateway.server_port}', 'pin': pin}
        self.clients = {}
        self.identities = {}
        for name, platform, address in peers:
            token = secrets.token_urlsafe(32)
            gateway.lab_sources[token] = address
            connection = PinnedHub(endpoint, token)
            self.clients[name] = connection
            self.addCleanup(self.close_connection, connection)
            self.identities[name] = self.call(name, 'register', name=name, platform=platform, live_version=1)['id']
            self.call(name, 'availability', enabled=True)

    @staticmethod
    def close_connection(hub):
        if hub.connection:
            hub.connection.close()

    def call(self, device, operation, **body):
        return self.clients[device].call(operation, body)

    def approve(self, target, request, *, persistent=False):
        response = self.call(target, 'approve', approval=request['approval'], allow=True, persistent=persistent)
        self.assertEqual('approved', response['state'])

    def pair(self, caller, target, *, persistent=False):
        code = self.call(target, 'code')['code']
        request = self.call(caller, 'connect', code=code)
        self.approve(target, request, persistent=persistent)

    def contacts(self, name):
        return {peer['id']: peer for peer in self.call(name, 'state')['peers']}

    def test_connection_codes_and_directional_approval_across_three_source_networks(self):
        mac, first, second = self.clients
        mac_id, first_id, second_id = (self.identities[name] for name in self.clients)
        code = self.call(first, 'code')['code']
        request = self.call(mac, 'connect', code=code)
        self.assertEqual(first_id, request['peer']['id'])
        self.assertEqual('pending', request['state'])
        with self.assertRaises(HubRejection):
            self.call(second, 'connect', code=code)
        with self.assertRaises(HubRejection):
            self.call(second, 'approve', approval=request['approval'], allow=True, persistent=True)
        with self.assertRaises(HubRejection):
            self.call(mac, 'live-open', peer=first_id)

        self.approve(first, request)
        self.assertTrue(self.contacts(mac)[first_id]['can_control'])
        self.assertFalse(self.contacts(first)[mac_id]['can_control'])
        reverse = self.call(first, 'access', peer=mac_id)
        self.approve(mac, reverse, persistent=True)
        self.assertTrue(self.contacts(mac)[first_id]['trusted_incoming'])

        self.pair(first, second, persistent=True)
        self.assertNotIn(second_id, self.contacts(mac))
        self.pair(mac, second, persistent=True)
        self.assertEqual({first_id, second_id}, set(self.contacts(mac)))
        self.assertEqual({mac_id, second_id}, set(self.contacts(first)))
        self.assertEqual({mac_id, first_id}, set(self.contacts(second)))

        expired_code = self.call(second, 'code')['code']
        self.now += 3601
        self.assertFalse(self.contacts(mac)[first_id]['can_control'])
        self.assertTrue(self.contacts(first)[mac_id]['can_control'])
        self.assertTrue(self.contacts(mac)[second_id]['can_control'])
        with self.assertRaises(HubRejection):
            self.call(first, 'connect', code=expired_code)

    def test_two_active_targets_isolate_frames_inputs_and_revocation(self):
        mac, first, second = self.clients
        self.pair(mac, first)
        self.pair(mac, second, persistent=True)
        sessions = {device: self.call(mac, 'live-open', peer=self.identities[device])['id']
                    for device in (first, second)}
        self.assertNotEqual(sessions[first], sessions[second])
        self.assertEqual([], self.call(mac, 'live-agent-state')['sessions'])
        with self.assertRaises(HubRejection):
            self.call(second, 'live-view', id=sessions[first])
        frames_published = threading.Barrier(2, timeout=5)

        def publish_and_deliver(device, position):
            identifier = sessions[device]
            geometry = f'{position},0,1280,720'
            jpeg = base64.b64encode(b'\xff\xd8' + device.encode() + b'\xff\xd9').decode()
            frame = {'seq': 1, 'geometry': geometry, 'jpeg_base64': jpeg}
            self.assertEqual([{'id': identifier}], self.call(device, 'live-agent-state')['sessions'])
            self.assertTrue(self.call(device, 'live-frame', id=identifier, frame=frame)['active'])
            frames_published.wait()
            self.assertEqual(frame, self.call(mac, 'live-view', id=identifier)['frame'])
            self.call(mac, 'live-input', id=identifier, seq=1, geometry=geometry,
                      events=[{'type': 'move', 'x': position, 'y': position}])
            inputs = self.call(device, 'live-exchange', id=identifier, ack=0)['inputs']
            self.assertEqual([1], [item['seq'] for item in inputs])
            self.assertEqual([{'type': 'move', 'x': position, 'y': position}], inputs[0]['events'])
            self.call(device, 'live-exchange', id=identifier, ack=1)

        with ThreadPoolExecutor(max_workers=2) as workers:
            jobs = [workers.submit(publish_and_deliver, device, pos)
                    for device, pos in ((first, 10), (second, 20))]
            for job in jobs:
                job.result(timeout=5)

        self.call(first, 'disconnect', peer=self.identities[mac])
        self.assertEqual([], self.call(first, 'live-agent-state')['sessions'])
        with self.assertRaises(HubRejection):
            self.call(mac, 'live-view', id=sessions[first])
        with self.assertRaises(HubRejection):
            self.call(first, 'live-exchange', id=sessions[first], ack=1)
        self.assertFalse(self.contacts(mac)[self.identities[first]]['can_control'])
        self.assertTrue(self.contacts(mac)[self.identities[second]]['can_control'])
        remaining = self.call(mac, 'live-view', id=sessions[second])
        self.assertEqual(1, remaining['ack'])
        self.assertEqual('20,0,1280,720', remaining['frame']['geometry'])
        self.assertTrue(self.call(second, 'live-exchange', id=sessions[second], ack=1)['active'])


if __name__ == '__main__':
    unittest.main()
