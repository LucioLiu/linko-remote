"""Standalone pinned-TLS rendezvous for symmetric Linko clients.

Kept separate from the 0.7 invitation/worker gateway so preview testing cannot
restart, migrate, or replace an existing pairing or in-flight desktop action.
"""
import argparse
import http.server
import json
import re
import select
import socket
import ssl
import sys
import threading
import time
from pathlib import Path

from .live import LIVE_STREAM_RECORD_MAX, LIVE_STREAM_SECONDS, LIVE_STREAM_TYPE
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
        # Keep the command and its receipt queries on one connection. Infrequent
        # UI changes still use short-lived connections.
        if self.path not in ('/v2/poll', '/v2/state', '/v2/result', '/v2/register', '/v2/submit', '/v2/recover',
                              '/v2/live-open', '/v2/live-view', '/v2/live-input', '/v2/live-exchange', '/v2/live-agent-state', '/v2/live-frame'):
            self.close_connection = True
            self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        operations = {'register', 'name', 'availability', 'code', 'connect', 'access',
                      'approve', 'disconnect', 'state', 'poll', 'submit', 'result', 'recover',
                      'live-open', 'live-close', 'live-view', 'live-input', 'live-exchange', 'live-agent-state', 'live-frame',
                      'live-stream'}
        operation = self.path[4:] if self.path.startswith('/v2/') else ''
        if operation not in operations:
            self.close_connection = True
            return self.answer(404, {'ok': False, 'error': 'Not found'})
        authorization = self.headers.get_all('Authorization', [])
        if len(authorization) != 1 or not re.fullmatch('Bearer [A-Za-z0-9_-]{40,90}', authorization[0]):
            self.close_connection = True
            return self.answer(401, {'ok': False, 'error': '身份验证失败'})
        maximum = 24_000_000 if operation == 'result' else 1_500_000 if operation in ('live-exchange', 'live-frame') else 16000
        lengths = self.headers.get_all('Content-Length', [])
        if len(lengths) != 1 or not re.fullmatch('[0-9]{1,8}', lengths[0]) or not 1 <= int(lengths[0]) <= maximum or self.headers.get('Transfer-Encoding'):
            self.close_connection = True
            return self.answer(400, {'ok': False, 'error': '请求无效'})
        try:
            raw = self.rfile.read(int(lengths[0]))
            if len(raw) != int(lengths[0]):
                raise ValueError('Incomplete body')
            body = json.loads(raw)
            if operation == 'live-stream':
                if not isinstance(body, dict):
                    raise ValueError('Invalid stream request')
                return self.stream_live(authorization[0][7:], body.get('id'), body.get('after'))
            value = self.server.hub.dispatch(operation, authorization[0][7:], body, self.client_address[0])
            if operation == 'live-open':
                value['view_stream'] = 1
            return self.answer(200, {'ok': True, 'value': value})
        except PermissionError as error:
            if operation == 'submit':
                print('Submit rejected:', type(error).__name__, str(error)[:150], file=sys.stderr, flush=True)
            return self.answer(403, {'ok': False, 'error': str(error)})
        except (ValueError, KeyError, TypeError) as error:
            if operation.startswith('live-'):
                print('Live request rejected:', operation, type(error).__name__, str(error)[:100], file=sys.stderr, flush=True)
            if operation == 'submit':
                print('Submit rejected:', type(error).__name__, str(error)[:150], file=sys.stderr, flush=True)
            return self.answer(400, {'ok': False, 'error': '请求无效或上一操作尚未确认'})

    def stream_live(self, credential, identifier, confirmed):
        if not self.server.stream_capacity.acquire(blocking=False):
            return self.answer(503, {'ok': False, 'error': '连续画面连接繁忙'})
        try:
            live = self.server.hub.live
            session = live.begin_stream(credential, identifier)
            timeout = self.connection.gettimeout()
            try:
                # Bound a stalled write. No frame queue is maintained per peer.
                self.connection.settimeout(.5)
                self.send_response(200)
                self.send_header('Content-Type', LIVE_STREAM_TYPE)
                self.send_header('Transfer-Encoding', 'chunked')
                self.send_header('Cache-Control', 'no-store')
                started = time.monotonic()
                self.end_headers()

                # Reuse across batches only when this connection's viewer
                # explicitly confirms the sequence it consumed.
                after = confirmed if (type(confirmed) is int and
                                      getattr(self, 'stream_cursor', None) == (identifier, confirmed)) else None
                self.stream_cursor = None
                ack = None
                deadline = started + LIVE_STREAM_SECONDS
                heartbeat = time.monotonic() + .25
                while (now := time.monotonic()) < deadline:
                    # The client sends no bytes until the batch ends. Readability
                    # here indicates a disconnect or an out-of-protocol request.
                    if self.connection.pending() or select.select([self.connection], [], [], 0)[0]:
                        self.close_connection = True
                        return
                    value = live.stream_update(session, after, ack, min(.1, deadline - now))
                    if value is None:
                        if now >= heartbeat:
                            self.stream_chunk(b'\n')
                            heartbeat = time.monotonic() + .25
                        continue
                    value['sent_ms'] = int((now - started) * 1000)
                    self.stream_chunk(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode() + b'\n')
                    heartbeat = time.monotonic() + .25
                    if value['error']:
                        break
                    if value['frame']:
                        after = value['frame']['seq']
                    elif not value.get('unchanged'):
                        after = None
                    ack = value['ack']
                self.wfile.write(b'0\r\n\r\n')
                self.stream_cursor = (identifier, after)
            finally:
                self.connection.settimeout(timeout)
                live.end_stream(session)
        finally:
            self.server.stream_capacity.release()

    def stream_chunk(self, record):
        if len(record) > LIVE_STREAM_RECORD_MAX:
            raise OSError('Live stream record too large')
        self.wfile.write(f'{len(record):x}\r\n'.encode('ascii') + record + b'\r\n')


class PeerGateway(http.server.ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, home, cert, key, port, bind='127.0.0.1'):
        self.hub = PeerHub(Path(home) / 'peers.sqlite')
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.context.load_cert_chain(str(cert), str(key))
        self.capacity = threading.BoundedSemaphore(64)
        self.stream_capacity = threading.BoundedSemaphore(24)
        super().__init__((bind, port), Handler)

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
    parser.add_argument('--bind', default='127.0.0.1')
    args = parser.parse_args()
    with PeerGateway(args.home, args.cert, args.key, args.port, args.bind) as server:
        server.serve_forever()


if __name__ == '__main__':
    main()
