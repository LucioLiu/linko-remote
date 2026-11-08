"""Expose only an authenticated worker's poll and result routes to a TCP tunnel.

Both TLS endpoints live on the controller. The outside TCP relay cannot see
device credentials, request bodies, or the controller's administrative routes.
"""
import argparse
import hmac
import http.client
import http.server
import json
import socket
import ssl
import threading
from pathlib import Path
from urllib.parse import urlsplit

from .core import Bridge
from .transport import PinnedConnection

MAX_BODY = 24_000_000


class WorkerHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        # Headers and query strings may identify an enrolled computer.
        pass

    def dispatch(self):
        config = self.worker_config if hasattr(self, "worker_config") else self.server.config
        path = urlsplit(self.path)
        if not ((self.command == "GET" and path.path == "/poll")
                or (self.command == "POST" and self.path == "/result")):
            self.close_connection = True
            self.send_error(404)
            return
        expected = ("Bearer " + config["device"]).encode()
        supplied = self.headers.get_all("Authorization", [])
        if len(supplied) != 1 or not hmac.compare_digest(supplied[0].encode(), expected):
            self.close_connection = True
            self.send_error(401)
            return
        lengths = self.headers.get_all("Content-Length", [])
        try:
            length = int(lengths[0]) if lengths else 0
        except ValueError:
            length = -1
        if (self.headers.get("Transfer-Encoding") or len(lengths) > 1
                or not 0 <= length <= MAX_BODY
                or (self.command == "POST") != (length > 0)):
            self.close_connection = True
            self.send_error(400)
            return
        body = self.rfile.read(length) if length else None
        if body is not None and len(body) != length:
            self.close_connection = True
            return
        upstream = PinnedConnection(config["port"], config["pin"])
        try:
            upstream.request(self.command, self.path, body, {
                "Authorization": supplied[0], "Content-Type": "application/json"
            })
            result = upstream.getresponse()
            payload = result.read()
            self.send_response(result.status)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)
        except (OSError, http.client.HTTPException):
            self.close_connection = True
            # Never replay a POST when the upstream response was lost.
        finally:
            upstream.close()

    do_POST = dispatch
    do_GET = dispatch


class WorkerGateway(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, root, port):
        self.config = json.loads((root / "config.json").read_text())
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.context.load_cert_chain(root / "server.crt", root / "server.key")
        self.capacity = threading.BoundedSemaphore(24)
        super().__init__(("127.0.0.1", port), WorkerHandler)

    def process_request(self, request, client_address):
        if not self.capacity.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.capacity.release()
            raise

    def process_request_thread(self, request, address):
        try:
            request.settimeout(8)
            with self.context.wrap_socket(request, server_side=True) as secured:
                secured.settimeout(30)
                secured.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                self.finish_request(secured, address)
        except OSError:
            pass
        finally:
            self.shutdown_request(request)
            self.capacity.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", required=True)
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    root = Path(Bridge().device(args.device)["runtime"])
    with WorkerGateway(root, args.port) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
