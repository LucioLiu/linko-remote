"""The Mac viewer uses the same lease and acknowledgement stream after fallback."""
import http.client
import http.server
import json
import threading
import unittest
from unittest.mock import patch

from lanpilot.peer_client import LocalHandler
import test_live_stream as fixture


class MacLiveViewerTests(unittest.TestCase):
    def test_negotiation_ordered_media_and_jpeg_fallback(self):
        loopback = fixture.LiveStreamTLSTests()
        self.addCleanup(loopback.doCleanups)
        loopback.setUp()
        loopback.sender.call('register', {'name': 'Fixture', 'platform': 'windows',
                                          'live_version': 1, 'live_avc': 1})
        batch_duration = patch('lanpilot.peer_gateway.LIVE_STREAM_SECONDS', .4)
        batch_duration.start()
        self.addCleanup(batch_duration.stop)

        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), LocalHandler)
        server.client = loopback.client
        server.authority = f'127.0.0.1:{server.server_port}'
        server.nonce = 'viewer-fixture'
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        session = loopback.client.api('live-open', {'peer': loopback.target,
                                                    'viewer_transport': 'stream', 'accept_avc': 1})
        identifier = session['id']
        self.assertEqual(1, session['live_avc'])
        endpoint = '/live-media/' + identifier
        connection = http.client.HTTPConnection(server.authority, timeout=3)
        self.addCleanup(connection.close)
        connection.request('GET', endpoint)
        with connection.getresponse() as response:
            self.assertEqual(403, response.status)
            response.read()

        connection.request('GET', endpoint, headers={'X-Linko-Session': server.nonce})
        with connection.getresponse() as response:
            self.assertEqual(200, response.status)
            loopback.wait_for(lambda: '/v2/live-stream' in loopback.relay.paths)
            for seq, unit in ((1, 0), (2, 1)):
                payload = fixture.avc_upload(seq, unit)
                loopback.upload_avc(identifier, payload)
                media = self.read_media(response)
                self.assertEqual(seq, media['frame']['seq'])
                self.assertEqual(payload['avc_base64'], media['frame']['avc_base64'])
                self.assertLess(media['frame_age_ms'], 1500)
                snapshot = loopback.client.api('live-view', {'id': identifier})
                self.assertNotIn('avc_base64', snapshot['frame'])
                loopback.wait_for(lambda: loopback.relay.paths.count('/v2/live-stream') >= 2)

            self.assertEqual({'accepted': True},
                             loopback.client.api('live-jpeg', {'id': identifier}))
            loopback.wait_for(lambda: '/v2/live-view' in loopback.relay.paths)
            # Codec-specific sequences can overlap on the same lease.
            loopback.send(identifier, 2)
            media = self.read_media(response)
            self.assertEqual(2, media['frame']['seq'])
            self.assertIn('jpeg_base64', media['frame'])
            self.assertEqual(0, loopback.sender.call('live-exchange', {'id': identifier})['live_avc'])

    @staticmethod
    def read_media(response):
        while line := response.readline():
            if line != b'\n':
                return json.loads(line)
        raise AssertionError('Local media feed closed')
