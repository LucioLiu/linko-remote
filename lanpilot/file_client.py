"""Local file consent, streaming checksums and verified-only downloads."""
from dataclasses import dataclass, field
import hashlib
import http.client
import os
from pathlib import Path
import secrets
import socket
import tempfile
import threading
import time

from .file_transfer import (ACTIVE, FILE_CHUNK, FILE_HISTORY_TTL, FILE_MAX,
                            FILE_PER_PEER, SAFE_ID, decode_chunk)


@dataclass
class Upload:
    item: dict
    expires: float
    sent: int = 0
    checksum: object = field(default_factory=hashlib.sha256)
    lock: object = field(default_factory=threading.Lock)


@dataclass
class Receipt:
    item: dict
    path: Path
    cancel: threading.Event = field(default_factory=threading.Event)
    expires: float = 0
    ticket: str = ''
    ticket_expires: float = 0
    worker: object = None
    hub: object = None
    error: str = ''


class ClientFiles:
    def __init__(self, home, transport):
        self.spool = home / 'file-transfers'
        self.transport = transport
        self.control = transport()
        self.data = transport()
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.uploads = {}
        self.receipts = {}

    def clear_stale_parts(self):
        # The desktop instance lock protects the spool across app restarts.
        for path in self.spool.glob('receipt-*.part'):
            path.unlink(missing_ok=True)

    @staticmethod
    def unlink(path):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            return False
        return True

    def cancel_remote(self, transfer_id, failed=False, hub=None):
        try:
            (hub or self.control).call('file-cancel', {'id': transfer_id, 'failed': failed})
        except (OSError, ValueError, http.client.HTTPException):
            # If the relay is unreachable, its inactivity deadline clears the transfer.
            pass

    def prune(self):
        now = time.monotonic()
        with self.lock:
            for identifier, upload in list(self.uploads.items()):
                if upload.expires <= now:
                    del self.uploads[identifier]
            for identifier, receipt in list(self.receipts.items()):
                if receipt.worker is None and receipt.expires <= now and self.unlink(receipt.path):
                    del self.receipts[identifier]

    def snapshot(self, remote):
        self.prune()
        with self.lock:
            items = {item['id']: dict(item) for item in remote}
            for receipt in self.receipts.values():
                if receipt.worker is not None:
                    continue
                if receipt.expires <= time.monotonic():
                    continue
                identifier = receipt.item['id']
                if receipt.error:
                    latest = items.get(identifier, receipt.item)
                    if latest.get('phase') not in ('declined', 'cancelled'):
                        items[identifier] = dict(latest, phase='failed', error=receipt.error)
                elif not receipt.cancel.is_set():
                    items[identifier] = dict(receipt.item, phase='complete',
                                             received=receipt.item['size'], downloadable=True)
            return list(items.values())

    def api(self, operation, body, compatible):
        if self.stop.is_set():
            raise ValueError('邻控正在退出')
        if not compatible and operation != 'file-download':
            raise ValueError('当前连接服务尚未支持文件传输')
        if operation == 'file-offer':
            item = self.control.call(operation, {k: body.get(k) for k in ('peer', 'name', 'size')})
            with self.lock:
                self.uploads[item['id']] = Upload(item, time.monotonic() + FILE_HISTORY_TTL)
            return item
        identifier = body.get('id')
        if not isinstance(identifier, str) or not SAFE_ID.fullmatch(identifier):
            raise ValueError('文件请求无效')
        if operation == 'file-status':
            return self.control.call(operation, {'id': identifier})
        if operation == 'file-accept':
            return self.accept(identifier)
        if operation == 'file-cancel':
            with self.lock:
                self.uploads.pop(identifier, None)
                receipt = self.receipts.get(identifier)
                if receipt and receipt.worker is not None:
                    receipt.cancel.set()
            return self.control.call(operation, {'id': identifier, 'failed': body.get('failed') is True})
        if operation == 'file-download':
            with self.lock:
                receipt = self.receipts.get(identifier)
                if not receipt or receipt.worker is not None or receipt.error or receipt.cancel.is_set() or receipt.expires <= time.monotonic():
                    raise ValueError('文件已过期或尚未接收完毕')
                receipt.ticket = secrets.token_urlsafe(32)
                receipt.ticket_expires = time.monotonic() + 90
                return {'url': '/file-download/' + receipt.ticket}
        if operation in ('file-push', 'file-seal'):
            return self.send(operation, identifier, body)
        raise ValueError('无效的文件操作')

    def send(self, operation, identifier, body):
        with self.lock:
            upload = self.uploads.get(identifier)
        if upload is None:
            raise ValueError('发送已取消或原文件页面已关闭')
        with upload.lock:
            if operation == 'file-seal':
                if upload.sent != upload.item['size']:
                    raise ValueError('文件尚未发送完整')
                result = self.data.call(operation, {'id': identifier, 'sha256': upload.checksum.hexdigest()})
                with self.lock:
                    self.uploads.pop(identifier, None)
                return result
            offset = body.get('offset')
            if type(offset) is not int or offset != upload.sent:
                raise ValueError('文件分块顺序无效')
            data = decode_chunk(body.get('data'))
            result = self.data.call(operation, {'id': identifier, 'offset': offset, 'data': body['data']})
            if result['accepted']:
                upload.checksum.update(data)
                upload.sent += len(data)
                upload.expires = time.monotonic() + FILE_HISTORY_TTL
            return result

    def accept(self, identifier):
        self.prune()
        hub = self.transport()
        item = hub.call('file-status', {'id': identifier})
        if item['phase'] != 'offered':
            raise ValueError('文件请求已失效')
        with self.lock:
            if self.stop.is_set() or identifier in self.receipts:
                raise ValueError('文件请求已处理')
            reserved = [receipt for receipt in self.receipts.values()
                        if receipt.worker is not None or receipt.path.exists()]
            if len(reserved) >= FILE_PER_PEER or sum(r.item['size'] for r in reserved) + item['size'] > 2 * FILE_MAX:
                raise ValueError('接收空间已达上限，请等待已接收文件过期后重试')
            self.spool.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd, path = tempfile.mkstemp(prefix='receipt-', suffix='.part', dir=self.spool)
            receipt = Receipt(item, Path(path), hub=hub)
            worker = threading.Thread(target=self.receive, args=(hub, receipt, fd), daemon=True)
            receipt.worker = worker
            self.receipts[identifier] = receipt
            try:
                worker.start()
            except BaseException:
                del self.receipts[identifier]
                os.close(fd)
                self.unlink(receipt.path)
                raise
        return {'accepted': True}

    def receive(self, hub, receipt, fd):
        identifier = receipt.item['id']
        size = receipt.item['size']
        read = 0
        checksum = hashlib.sha256()
        complete = False
        try:
            with os.fdopen(fd, 'wb') as output:
                if receipt.cancel.is_set() or self.stop.is_set():
                    return
                hub.call('file-accept', {'id': identifier})
                while not receipt.cancel.is_set() and not self.stop.is_set():
                    chunk = hub.call('file-pull', {'id': identifier, 'offset': read})
                    if 'data' in chunk:
                        data = decode_chunk(chunk['data'])
                        if chunk.get('offset') != read or len(data) != min(FILE_CHUNK, size - read):
                            raise ValueError('接收文件的分块顺序或长度无效')
                        output.write(data)
                        checksum.update(data)
                        read += len(data)
                    elif chunk.get('sha256'):
                        if read != size or chunk['sha256'] != checksum.hexdigest():
                            raise ValueError('接收文件完整性校验失败')
                        output.flush()
                        os.fsync(output.fileno())
                        break
                    else:
                        receipt.cancel.wait(.1)
                else:
                    return
            if not receipt.cancel.is_set() and not self.stop.is_set():
                self.confirm(hub, identifier, checksum.hexdigest())
                complete = True
        except (OSError, ValueError, http.client.HTTPException) as error:
            if not receipt.cancel.is_set() and not self.stop.is_set():
                receipt.error = str(error) or '传输中断或校验失败'
                self.cancel_remote(identifier, failed=True, hub=hub)
        finally:
            if not complete:
                self.unlink(receipt.path)
            with self.lock:
                if receipt.cancel.is_set() or self.stop.is_set():
                    self.unlink(receipt.path)
                receipt.expires = time.monotonic() + FILE_HISTORY_TTL
                receipt.worker = receipt.hub = None
            connection = getattr(hub, 'connection', None)
            if connection:
                connection.close()

    @staticmethod
    def confirm(hub, identifier, digest):
        try:
            hub.call('file-complete', {'id': identifier, 'sha256': digest})
        except (OSError, http.client.HTTPException) as error:
            if hub.call('file-status', {'id': identifier})['phase'] != 'complete':
                raise error

    def open_download(self, ticket):
        self.prune()
        with self.lock:
            now = time.monotonic()
            for receipt in self.receipts.values():
                if receipt.ticket == ticket and receipt.ticket_expires > now and receipt.expires > now:
                    return receipt.path.open('rb'), receipt.item
        raise ValueError('下载链接已失效，请重新选择下载')

    def close(self):
        self.stop.set()
        for hub in (self.control, self.data):
            connection = getattr(hub, 'connection', None)
            if connection:
                connection.close()
        with self.lock:
            workers = []
            for receipt in self.receipts.values():
                receipt.cancel.set()
                if receipt.worker is None:
                    self.unlink(receipt.path)
                    continue
                workers.append(receipt.worker)
                connection = getattr(receipt.hub, 'connection', None)
                sock = getattr(connection, 'sock', None)
                if sock:
                    try:
                        sock.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
        for worker in workers:
            worker.join(timeout=.5)
