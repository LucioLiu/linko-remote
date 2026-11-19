"""Standalone pinned-TLS rendezvous for symmetric Linko clients.

Kept separate from the 0.7 invitation/worker gateway so preview testing cannot
restart, migrate, or replace an existing pairing or in-flight desktop action.
"""
import argparse
import http.server
import json
import re
import socket
import ssl
import sys
import threading
from pathlib import Path

from .peer_hub import PeerHub


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_):
        pass

    def answer(self, status, value):
        raw = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        # UI requests may be minutes apart. Public TCP relays can retire an
        # idle connection without sending a close response to its client.
        # Close after each UI operation; continuous agent polling stays pooled.
        if self.path not in ('/v2/poll', '/v2/state', '/v2/result', '/v2/register'):
            self.close_connection = True
            self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        operations = {'register', 'name', 'availability', 'code', 'connect', 'access',
                      'approve', 'disconnect', 'state', 'poll', 'submit', 'result', 'recover'}
        operation = self.path[4:] if self.path.startswith('/v2/') else ''
        if operation not in operations:
            self.close_connection = True
            return self.answer(404, {'ok': False, 'error': 'Not found'})
        authorization = self.headers.get_all('Authorization', [])
        if len(authorization) != 1 or not re.fullmatch('Bearer [A-Za-z0-9_-]{40,90}', authorization[0]):
            self.close_connection = True
            return self.answer(401, {'ok': False, 'error': '身份验证失败'})
        maximum = 24_000_000 if operation == 'result' else 16000
        lengths = self.headers.get_all('Content-Length', [])
        if len(lengths) != 1 or not re.fullmatch('[0-9]{1,8}', lengths[0]) or not 1 <= int(lengths[0]) <= maximum or self.headers.get('Transfer-Encoding'):
            self.close_connection = True
            return self.answer(400, {'ok': False, 'error': '请求无效'})
        try:
            raw = self.rfile.read(int(lengths[0]))
            if len(raw) != int(lengths[0]):
                raise ValueError('Incomplete body')
            value = self.server.hub.dispatch(operation, authorization[0][7:], json.loads(raw), self.client_address[0])
            return self.answer(200, {'ok': True, 'value': value})
        except PermissionError as error:
            if operation == 'submit':
                print('Submit rejected:', type(error).__name__, str(error)[:150], file=sys.stderr, flush=True)
            return self.answer(403, {'ok': False, 'error': str(error)})
        except (ValueError, KeyError, TypeError) as error:
            if operation == 'submit':
                print('Submit rejected:', type(error).__name__, str(error)[:150], file=sys.stderr, flush=True)
            return self.answer(400, {'ok': False, 'error': '请求无效或上一操作尚未确认'})


class PeerGateway(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, home, cert, key, port):
        self.hub = PeerHub(Path(home) / 'peers.sqlite')
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.context.load_cert_chain(str(cert), str(key))
        self.capacity = threading.BoundedSemaphore(64)
        super().__init__(('127.0.0.1', port), Handler)

    def process_request(self, request, address):
        if not self.capacity.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
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
    parser = argparse.ArgumentParser()
    parser.add_argument('--home', type=Path, required=True)
    parser.add_argument('--cert', type=Path, required=True)
    parser.add_argument('--key', type=Path, required=True)
    parser.add_argument('--port', type=int, required=True)
    args = parser.parse_args()
    with PeerGateway(args.home, args.cert, args.key, args.port) as server:
        server.serve_forever()


if __name__ == '__main__':
    main()
