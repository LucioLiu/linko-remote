import contextlib
import hashlib
import http.client
import io
import json
import ssl
import tempfile
import threading
import unittest
from pathlib import Path
from socket import socket

from lanpilot.broker import start_listener
from lanpilot.worker_gateway import WorkerGateway
from relay import init_runtime


class WorkerGatewayTest(unittest.TestCase):
    def test_network_peer_can_use_only_its_worker_routes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with socket() as probe:
                probe.bind(("127.0.0.1", 0))
                backend_port = probe.getsockname()[1]
            with contextlib.redirect_stdout(io.StringIO()):
                init_runtime(root, "127.0.0.1", backend_port)
            config = json.loads((root / "config.json").read_text())
            backend = start_listener(root, config)
            self.addCleanup(backend.server_close)
            self.addCleanup(backend.shutdown)
            gateway = WorkerGateway(root, 0)
            self.addCleanup(gateway.server_close)
            self.addCleanup(gateway.shutdown)
            threading.Thread(target=gateway.serve_forever, daemon=True).start()

            def request(target, method, path, token, body=None):
                connection = http.client.HTTPSConnection(
                    "127.0.0.1", target, context=ssl._create_unverified_context(),
                    timeout=4,
                )
                try:
                    connection.connect()
                    pin = hashlib.sha256(connection.sock.getpeercert(True)).hexdigest()
                    self.assertEqual(pin, config["pin"])
                    connection.request(method, path, body=json.dumps(body) if body else None,
                                       headers={"Authorization": "Bearer " + token})
                    response = connection.getresponse()
                    return response.status, response.read()
                finally:
                    connection.close()

            g = gateway.server_port
            rid = "a" * 32
            self.assertEqual(request(g, "GET", "/health", config["admin"])[0], 404)
            self.assertEqual(request(g, "POST", "/submit", config["admin"], {"id": rid})[0], 404)
            self.assertEqual(request(g, "GET", "/poll", "wrong-token")[0], 401)
            self.assertEqual(request(g, "GET", "/poll", config["admin"])[0], 401)
            self.assertEqual(request(backend_port, "POST", "/submit", config["admin"],
                                     {"id": rid, "request": {"action": "observe"}})[0], 200)
            status, data = request(g, "GET", "/poll?session=" + "c" * 32, config["device"])
            self.assertEqual((status, json.loads(data)["id"]), (200, rid))
            result = {"request_id": rid, "ok": True}
            self.assertEqual(request(g, "POST", "/result", config["device"], result)[0], 200)
            status, data = request(backend_port, "GET", "/result/" + rid, config["admin"])
            self.assertEqual((status, json.loads(data)), (200, result))


if __name__ == "__main__":
    unittest.main()
