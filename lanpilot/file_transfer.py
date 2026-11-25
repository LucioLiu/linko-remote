"""Versioned, consent-based single-file transfer over the paired TLS relay."""
import base64
from dataclasses import dataclass, field
import hashlib
import re
import secrets
import unicodedata


FILE_VERSION = 1
FILE_CHUNK = 512 * 1024
FILE_MAX = 100 * 1024 * 1024
FILE_CHUNK_BODY = 4 * ((FILE_CHUNK + 2) // 3) + 1024
FILE_OFFER_TTL = 120
FILE_IDLE_TTL = 45
FILE_HISTORY_TTL = 300
FILE_PER_PEER = 4
ACTIVE = ('offered', 'transferring')
FILE_OPERATIONS = frozenset(('file-offer', 'file-status', 'file-accept', 'file-cancel',
                             'file-push', 'file-pull', 'file-seal', 'file-complete'))
SAFE_ID = re.compile('[a-f0-9]{32}')
SHA256 = re.compile('[a-f0-9]{64}')


def file_name(value):
    if not isinstance(value, str):
        raise ValueError('文件名无效')
    # Use the same display and download name on both platforms.
    name = ''.join('_' if char in '/\\<>:"|?*' or unicodedata.category(char).startswith('C')
                   else char for char in value).strip(' .')
    if not name or len(name.encode('utf-8')) > 240:
        raise ValueError('文件名无效或过长')
    stem = name.split('.')[0].rstrip().upper()
    if re.fullmatch(r'CON|PRN|AUX|NUL|(?:COM|LPT)[0-9¹²³]', stem):
        name = '_' + name
    return name


def decode_chunk(value):
    if not isinstance(value, str) or not 1 <= len(value) <= FILE_CHUNK_BODY - 1024:
        raise ValueError('文件分块大小无效')
    data = base64.b64decode(value, validate=True)
    if not 0 < len(data) <= FILE_CHUNK:
        raise ValueError('文件分块大小无效')
    return data


@dataclass
class Transfer:
    id: str
    sender: str
    receiver: str
    name: str
    size: int
    expires: float
    phase: str = 'offered'
    uploaded: int = 0
    received: int = 0
    chunk: str = ''
    chunk_size: int = 0
    digest: str = ''
    checksum: object = field(default_factory=hashlib.sha256)
    error: str = ''

    def public(self):
        return {key: getattr(self, key) for key in (
            'id', 'sender', 'receiver', 'name', 'size', 'phase', 'received', 'error')}

    def finish(self, phase, now, error=''):
        self.phase, self.error = phase, error
        self.chunk, self.chunk_size = '', 0
        self.checksum = None
        self.expires = now + FILE_HISTORY_TTL


class RelayFiles:
    """All state is memory-only and guarded by the relay's existing lock."""

    def __init__(self):
        self.items = {}
        self.capabilities = {}

    def advertise(self, device, version, now):
        if type(version) is int and version == FILE_VERSION:
            self.capabilities[device] = now + FILE_IDLE_TTL
        elif self.capabilities.pop(device, None) is not None:
            for item in self.items.values():
                if device in (item.sender, item.receiver) and item.phase in ACTIVE:
                    item.finish('failed', now, '对方版本已变更，请重新尝试')

    def supports(self, device, now):
        return self.capabilities.get(device, 0) > now

    def prune(self, now):
        for device, expires in list(self.capabilities.items()):
            if expires <= now:
                del self.capabilities[device]
        for identifier, item in list(self.items.items()):
            if item.expires <= now:
                if item.phase in ACTIVE:
                    message = '等待对方确认超时' if item.phase == 'offered' else '传输中断或超时'
                    item.finish('failed', now, message)
                else:
                    del self.items[identifier]

    def snapshot(self, me):
        items = [item for item in reversed(self.items.values()) if me in (item.sender, item.receiver)]
        items.sort(key=lambda item: item.phase not in ACTIVE)
        return [item.public() for item in items[:24]]

    def offer(self, me, body, now, eligible):
        peer = body.get('peer')
        if not isinstance(peer, str) or peer == me or not eligible(peer):
            raise PermissionError('只能向已配对的电脑发送文件')
        if not self.supports(me, now) or not self.supports(peer, now):
            raise PermissionError('对方离线或当前版本不支持文件传输')
        size = body.get('size')
        if type(size) is not int or not 0 <= size <= FILE_MAX:
            raise PermissionError('每份文件最大为 100 MiB')
        active = [item for item in self.items.values() if item.phase in ACTIVE]
        if (len(self.items) >= 512 or len(active) >= 64 or any(
                sum(device in (item.sender, item.receiver) for item in active) >= FILE_PER_PEER
                for device in (me, peer))):
            raise PermissionError('文件传输数量已达上限，请稍后再试')
        identifier = secrets.token_hex(16)
        item = Transfer(identifier, me, peer, file_name(body.get('name')), size, now + FILE_OFFER_TTL)
        self.items[identifier] = item
        return item.public()

    def dispatch(self, operation, me, body, now):
        identifier = body.get('id')
        item = self.items.get(identifier) if isinstance(identifier, str) else None
        if item is None or me not in (item.sender, item.receiver):
            raise PermissionError('文件传输已结束或不可用')
        if operation == 'file-status':
            return item.public()
        if operation == 'file-cancel':
            if item.phase in ACTIVE:
                declined = item.phase == 'offered' and me == item.receiver
                reason = '接收方已拒绝' if declined else '传输已取消'
                if body.get('failed') is True:
                    reason = '传输中断或校验失败'
                item.finish('declined' if declined else 'failed' if body.get('failed') is True else 'cancelled',
                            now, reason)
            return item.public()
        sender_action = operation in ('file-push', 'file-seal')
        if me != (item.sender if sender_action else item.receiver):
            raise PermissionError('此操作需要由文件发送方或接收方在本机确认')
        if operation == 'file-accept':
            if item.phase != 'offered':
                raise PermissionError('文件请求已失效')
            item.phase, item.expires = 'transferring', now + FILE_IDLE_TTL
            return item.public()
        if item.phase != 'transferring':
            raise PermissionError(item.error or '接收方尚未同意，或文件传输已结束')
        if operation == 'file-push':
            return self.push(item, body, now)
        if operation == 'file-pull':
            return self.pull(item, body, now)
        if operation == 'file-seal':
            if item.uploaded != item.size or body.get('sha256') != item.checksum.hexdigest():
                item.finish('failed', now, '发送文件完整性校验失败')
                raise PermissionError(item.error)
            item.digest = body['sha256']
            item.expires = now + FILE_IDLE_TTL
            return item.public()
        if operation == 'file-complete':
            if not item.digest or body.get('sha256') != item.digest or item.received != item.size:
                item.finish('failed', now, '接收文件完整性校验失败')
                raise PermissionError(item.error)
            item.finish('complete', now)
            return item.public()
        raise ValueError('无效的文件操作')

    @staticmethod
    def push(item, body, now):
        offset = body.get('offset')
        if type(offset) is not int or offset != item.uploaded or item.digest:
            raise PermissionError('文件分块顺序无效')
        if item.chunk:
            return {'accepted': False}
        data = decode_chunk(body.get('data'))
        if len(data) != min(FILE_CHUNK, item.size - offset):
            raise PermissionError('文件分块大小与申报大小不符')
        item.checksum.update(data)
        item.chunk = body['data']
        item.chunk_size = len(data)
        item.uploaded += len(data)
        item.expires = now + FILE_IDLE_TTL
        return {'accepted': True}

    @staticmethod
    def pull(item, body, now):
        offset = body.get('offset')
        if type(offset) is not int or offset not in (item.received, item.uploaded):
            raise PermissionError('文件分块确认顺序无效')
        if item.chunk and offset > item.received:
            item.received += item.chunk_size
            item.chunk, item.chunk_size = '', 0
            item.expires = now + FILE_IDLE_TTL
        if item.chunk:
            return {'offset': item.received, 'data': item.chunk}
        return {'sha256': item.digest}
