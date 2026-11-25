"""Symmetric client directory and durable command receipts.

Every client can request a session and locally approve incoming sessions. A
pairing establishes a contact, never an implicit reverse control permission.
The gateway transports this API only over pinned TLS; device credentials stay
out of the UI and are stored as hashes at the hub.
"""
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from desktop import make_request
from .live import LIVE_WAIT_MS, LiveSessions
from .live_delta import encode_delta

READ_ONLY_TIMEOUTS = {'observe': 'SCREEN_CAPTURE_TIMED_OUT', 'status': 'STATUS_TIMED_OUT'}
BEFORE_DELIVERY = 'EXPIRED_OR_ACCESS_REVOKED_BEFORE_DELIVERY'
DESKTOP_PLATFORMS = frozenset(('macos', 'windows'))
DESKTOP_UNAVAILABLE = 'Linux 预览版可发起连接，暂未支持接收桌面控制'


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def display_name(value):
    if not isinstance(value, str):
        raise ValueError('请输入电脑名称')
    value = value.strip()
    if not 1 <= len(value) <= 100 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('电脑名称应为 1–100 个字符')
    return value


class PeerHub:
    def __init__(self, path, clock=time.time):
        self.clock = clock
        self.lock = threading.RLock()
        path = Path(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS devices (
          id TEXT PRIMARY KEY, credential TEXT UNIQUE, name TEXT, platform TEXT,
          seen REAL, incoming INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS codes (hash TEXT PRIMARY KEY, owner TEXT, expires REAL);
        CREATE TABLE IF NOT EXISTS approvals (
          id TEXT PRIMARY KEY, caller TEXT, target TEXT, expires REAL, state TEXT,
          created REAL);
        CREATE TABLE IF NOT EXISTS contacts (a TEXT, b TEXT, PRIMARY KEY(a,b));
        CREATE TABLE IF NOT EXISTS grants (
          caller TEXT, target TEXT, expires REAL, PRIMARY KEY(caller,target));
        CREATE TABLE IF NOT EXISTS commands (
          id TEXT PRIMARY KEY, caller TEXT, target TEXT, payload TEXT, expires REAL,
          delivered INTEGER DEFAULT 0, result TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS attempts (source TEXT, operation TEXT, at REAL);
        CREATE TABLE IF NOT EXISTS capabilities (id TEXT PRIMARY KEY, live INTEGER);
        CREATE TABLE IF NOT EXISTS live_avc_capabilities (id TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS migrated_receipts (id TEXT PRIMARY KEY, hash TEXT);
        ''')
        self.live = LiveSessions(self)

    def close(self):
        self.db.close()

    def _one(self, sql, args=()):
        return self.db.execute(sql, args).fetchone()

    def _rate(self, source, operation, maximum):
        now = self.clock()
        self.db.execute('DELETE FROM attempts WHERE at < ?', (now - 60,))
        count = self._one('SELECT COUNT(*) n FROM attempts WHERE source=? AND operation=?',
                          (digest(source), operation))['n']
        if count >= maximum:
            raise PermissionError('请求过于频繁，请稍候再试')
        self.db.execute('INSERT INTO attempts VALUES(?,?,?)', (digest(source), operation, now))

    def _device(self, credential):
        if not isinstance(credential, str) or not re.fullmatch(r'[A-Za-z0-9_-]{40,90}', credential):
            raise PermissionError('身份验证失败')
        row = self._one('SELECT * FROM devices WHERE credential=?', (digest(credential),))
        if row is None:
            raise PermissionError('身份验证失败')
        return row

    def _identity(self, row):
        value = {k: row[k] for k in ('id', 'name', 'platform')}
        value['accepts_desktop'] = row['platform'] in DESKTOP_PLATFORMS
        return value

    def _approval(self, row):
        result = dict(row)
        result['peer'] = self._identity(self._one('SELECT * FROM devices WHERE id=?', (row['caller'],)))
        return result

    def _finish_without_input(self, identifier, error):
        receipt = {'request_id': identifier, 'ok': False, 'input_attempted': False, 'error': error}
        self.db.execute('UPDATE commands SET result=? WHERE id=?', (json.dumps(receipt), identifier))
        return receipt

    def _expire_command(self, command, now):
        if command['expires'] > now:
            return None
        # After delivery, only a read-only request can be retired without a
        # worker receipt. An input might have run before the reply was lost.
        error = BEFORE_DELIVERY if not command['delivered'] else READ_ONLY_TIMEOUTS.get(
            json.loads(command['payload']).get('action'))
        if error:
            return self._finish_without_input(command['id'], error)
        return None

    def has_pending_command(self, target, now):
        command = self._one('SELECT * FROM commands WHERE target=? AND result IS NULL', (target,))
        return command is not None and self._expire_command(command, now) is None

    def dispatch(self, operation, credential, body, source='local'):
        if not isinstance(body, dict):
            raise ValueError('无效的请求')
        with self.lock:
            # Failed guesses count too. Commit rate accounting before opening
            # the operation transaction, which may legitimately roll back.
            if operation == 'register':
                self._rate(source, operation, 20)
            elif operation in ('connect', 'access'):
                self._rate(source, 'connect-source', 60)
                self._rate(str(credential), 'connect-client', 10)
            result = self._dispatch_committed(operation, credential, body, source)
            if operation in ('availability', 'disconnect'):
                self.live.prune(self.clock())
            wait_ms = body.get('wait_ms')
            if operation in ('live-view', 'live-exchange') and type(wait_ms) is int and wait_ms > 0:
                waited = self.live.wait_for_update(operation, body, result, min(wait_ms, LIVE_WAIT_MS))
                if waited is not None:
                    if operation == 'live-exchange':
                        # The initial call already committed the prior receipt and frame.
                        body = {'id': body['id'], 'ack': body.get('ack', 0)}
                    result = self._dispatch_committed(operation, credential, body, source)
                result['waited_ms'] = round((waited or 0) * 1000)
            work = self.live.prepare_view(body, result) if operation == 'live-view' else None

        if work is not None:
            session, base, current, _ = work
            try:
                delta = encode_delta(base['jpeg_base64'], current['jpeg_base64'], base['seq'])
                with self.lock:
                    self.live.finish_view_delta(result, work, delta)
            finally:
                with self.lock:
                    session['delta_pending'] = False
        return result

    def _dispatch_committed(self, operation, credential, body, source):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            result = self._dispatch(operation, credential, body, source)
            self.db.execute('COMMIT')
            return result
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def _dispatch(self, operation, credential, body, source):
        now = self.clock()
        if operation == 'register':
            if not isinstance(credential, str) or not re.fullmatch(r'[A-Za-z0-9_-]{40,90}', credential):
                raise PermissionError('身份验证失败')
            name = display_name(body.get('name'))
            platform = body.get('platform')
            if not isinstance(platform, str) or platform not in DESKTOP_PLATFORMS | {'linux'}:
                raise ValueError('不支持的系统')
            identifier = digest(credential)[:12]
            existing = self._one('SELECT * FROM devices WHERE id=?', (identifier,))
            if existing and not hmac.compare_digest(existing['credential'], digest(credential)):
                raise PermissionError('设备身份冲突')
            if existing and existing['platform'] != platform:
                raise PermissionError('设备系统身份不可更换')
            self.db.execute('INSERT INTO devices VALUES(?,?,?,?,?,0) '
                            'ON CONFLICT(id) DO UPDATE SET name=excluded.name,seen=excluded.seen',
                            (identifier, digest(credential), name, platform, now))
            live = platform in DESKTOP_PLATFORMS and body.get('live_version') == 1
            self.db.execute('INSERT OR REPLACE INTO capabilities VALUES(?,?)', (identifier, int(live)))
            if live and platform == 'windows' and type(body.get('live_avc')) is int and body['live_avc'] == 1:
                self.db.execute('INSERT OR IGNORE INTO live_avc_capabilities VALUES(?)', (identifier,))
            else:
                self.db.execute('DELETE FROM live_avc_capabilities WHERE id=?', (identifier,))
            return {'id': identifier, 'name': name, 'platform': platform}
        device = self._device(credential)
        me = device['id']
        if now - device['seen'] >= 2:
            self.db.execute('UPDATE devices SET seen=? WHERE id=?', (now, me))
        if operation.startswith('live-'):
            return self.live.dispatch(operation, me, body, now)
        if operation == 'name':
            name = display_name(body.get('name'))
            self.db.execute('UPDATE devices SET name=? WHERE id=?', (name, me))
            return {'name': name}
        if operation == 'availability':
            if type(body.get('enabled')) is not bool:
                raise ValueError('请明确选择是否允许连接')
            if body['enabled'] and device['platform'] not in DESKTOP_PLATFORMS:
                raise PermissionError(DESKTOP_UNAVAILABLE)
            self.db.execute('UPDATE devices SET incoming=? WHERE id=?', (body['enabled'], me))
            if not body['enabled']:
                self.db.execute('DELETE FROM grants WHERE target=? AND expires IS NOT NULL', (me,))
                self.db.execute('DELETE FROM codes WHERE owner=?', (me,))
                self.db.execute("UPDATE approvals SET state='denied' WHERE target=? AND state='pending'", (me,))
            return {'incoming': body['enabled']}
        if operation == 'code':
            if not device['incoming']:
                raise PermissionError('请先允许其他电脑连接本机')
            self.db.execute('DELETE FROM codes WHERE owner=? OR expires<?', (me, now))
            while True:
                code = ''.join(str(secrets.randbelow(10)) for _ in range(12))
                if not self._one('SELECT hash FROM codes WHERE hash=?', (digest(code),)):
                    break
            self.db.execute('INSERT INTO codes VALUES(?,?,?)', (digest(code), me, now + 600))
            return {'code': code, 'expires': now + 600}
        if operation in ('connect', 'access'):
            if operation == 'connect':
                code = body.get('code', '')
                if not isinstance(code, str) or not re.fullmatch(r'\d{12}', code):
                    raise PermissionError('验证码无效或已过期')
                record = self._one('SELECT * FROM codes WHERE hash=? AND expires>?', (digest(code), now))
                if record is None:
                    raise PermissionError('验证码无效或已过期')
                target = record['owner']
            else:
                target = body.get('peer')
                if not self._one('SELECT * FROM contacts WHERE a=? AND b=?', tuple(sorted((me, str(target))))):
                    raise PermissionError('请先使用验证码添加此电脑')
            peer = self._one('SELECT * FROM devices WHERE id=?', (target,))
            if target == me or not peer or not peer['incoming']:
                raise PermissionError('对方未允许连接')
            pending = self._one("SELECT * FROM approvals WHERE caller=? AND target=? AND state='pending' AND expires>?",
                                (me, target, now))
            if pending:
                return {'approval': pending['id'], 'state': 'pending'}
            identifier = uuid.uuid4().hex
            self.db.execute('INSERT INTO approvals VALUES(?,?,?,?,?,?)',
                            (identifier, me, target, now + 120, 'pending', now))
            if operation == 'connect':
                self.db.execute('DELETE FROM codes WHERE hash=?', (digest(body['code']),))
            return {'approval': identifier, 'state': 'pending', 'peer': self._identity(peer)}
        if operation == 'approve':
            persistent = body.get('persistent', False)
            if type(persistent) is not bool or (persistent and body.get('allow') is not True):
                raise ValueError('无效的连接授权选项')
            row = self._one("SELECT * FROM approvals WHERE id=? AND target=? AND state='pending' AND expires>?",
                            (body.get('approval'), me, now))
            if row is None or type(body.get('allow')) is not bool:
                raise PermissionError('此连接申请已失效')
            allow = body['allow'] and bool(device['incoming'])
            self.db.execute('UPDATE approvals SET state=? WHERE id=?',
                            ('approved' if allow else 'denied', row['id']))
            if allow:
                self.db.execute('INSERT OR IGNORE INTO contacts VALUES(?,?)', tuple(sorted((me, row['caller']))))
                expires = None if persistent else now + 3600
                self.db.execute('INSERT OR REPLACE INTO grants VALUES(?,?,?)', (row['caller'], me, expires))
            return {'state': 'approved' if allow else 'denied'}
        if operation == 'disconnect':
            peer = body.get('peer')
            self.db.execute('DELETE FROM grants WHERE (caller=? AND target=?) OR (caller=? AND target=?)',
                            (me, peer, peer, me))
            return {'disconnected': True}
        if operation == 'state':
            peers = self.db.execute('SELECT d.* FROM devices d JOIN contacts c ON '
                                   '(c.a=? AND c.b=d.id) OR (c.b=? AND c.a=d.id)', (me, me)).fetchall()
            result = []
            for peer in peers:
                item = self._identity(peer)
                cap = self._one('SELECT live FROM capabilities WHERE id=?', (peer['id'],))
                item['live'] = bool(cap and cap['live'])
                item.update(online=now - peer['seen'] < 20,
                            accepting_connections=bool(peer['incoming']),
                            trusted_incoming=bool(self._one('SELECT 1 FROM grants WHERE caller=? AND target=? AND expires IS NULL',
                                                            (peer['id'], me))),
                            can_control=bool(peer['incoming'] and self._one('SELECT 1 FROM grants WHERE caller=? AND target=? AND (expires IS NULL OR expires>?)',
                                                      (me, peer['id'], now))),
                            incoming=bool(device['incoming'] and self._one('SELECT 1 FROM grants WHERE caller=? AND target=? AND (expires IS NULL OR expires>?)',
                                                   (peer['id'], me, now))))
                result.append(item)
            approvals = self.db.execute("SELECT * FROM approvals WHERE target=? AND state='pending' AND expires>?",
                                        (me, now)).fetchall()
            outgoing = self.db.execute('SELECT id,state,target FROM approvals WHERE caller=? AND expires>?', (me, now)).fetchall()
            return {'self': self._identity(device), 'incoming': bool(device['incoming']), 'peers': result,
                    'supports_long_term_trust': True,
                    'approvals': [self._approval(row) for row in approvals],
                    'outgoing': [dict(row) for row in outgoing]}
        if operation == 'poll':
            self.db.execute('UPDATE devices SET seen=? WHERE id=?', (now, me))
            commands = self.db.execute('SELECT * FROM commands WHERE target=? AND delivered=0 AND result IS NULL ORDER BY created', (me,)).fetchall()
            for command in commands:
                permitted = device['incoming'] and self._one('SELECT 1 FROM grants WHERE caller=? AND target=? AND (expires IS NULL OR expires>?)',
                                                            (command['caller'], me, now))
                if command['expires'] <= now or not permitted:
                    self._finish_without_input(command['id'], BEFORE_DELIVERY)
                    continue
                self.db.execute('UPDATE commands SET delivered=1 WHERE id=?', (command['id'],))
                return {'id': command['id'], 'request': json.loads(command['payload']),
                        'valid_for_ms': max(1, int((command['expires'] - now) * 1000))}
            return None
        if operation == 'submit':
            peer = body.get('peer')
            self.live.prune(now)
            if any(s['target'] == peer for s in self.live.sessions.values()):
                raise ValueError('请先关闭连续桌面，再使用逐步操作')
            identifier = body.get('id')
            payload = body.get('request')
            if not isinstance(identifier, str) or not re.fullmatch('[a-f0-9]{32}', identifier) or not isinstance(payload, dict):
                raise ValueError('无效操作')
            if payload.get('action') not in ('observe', 'status', 'click', 'doubleclick', 'text', 'key', 'scroll'):
                raise PermissionError('本次连接仅允许取屏和键鼠操作')
            request = make_request(payload.get('action'), **{k: v for k, v in payload.items() if k not in ('action', 'expires')})
            request.pop('expires')
            encoded = json.dumps(request, sort_keys=True, separators=(',', ':'))
            prior = self._one('SELECT * FROM commands WHERE id=?', (identifier,))
            if prior:
                if prior['caller'] != me or prior['target'] != peer or prior['payload'] != encoded:
                    raise PermissionError('操作编号冲突')
                return {'id': identifier}
            target = self._one('SELECT * FROM devices WHERE id=?', (peer,))
            if not target or not target['incoming'] or not self._one('SELECT 1 FROM grants WHERE caller=? AND target=? AND (expires IS NULL OR expires>?)', (me, peer, now)):
                raise PermissionError('尚未取得对方授权')
            if self.has_pending_command(peer, now):
                raise ValueError('上一操作结果未确认，请先查询原结果')
            self.db.execute('INSERT INTO commands VALUES(?,?,?,?,?,0,NULL,?)', (identifier, me, peer, encoded, now + 45, now))
            return {'id': identifier}
        if operation == 'result':
            value = body.get('result')
            identifier = body.get('id')
            prior = self._one('SELECT * FROM commands WHERE id=? AND target=? AND delivered=1', (identifier, me))
            if prior is None or not isinstance(value, dict) or value.get('request_id') != identifier or type(value.get('ok')) is not bool or type(value.get('input_attempted')) is not bool:
                raise PermissionError('无效操作回执')
            encoded = json.dumps(value, sort_keys=True, separators=(',', ':'))
            migrated = self._one('SELECT hash FROM migrated_receipts WHERE id=?', (identifier,))
            if migrated and hmac.compare_digest(migrated['hash'], hashlib.sha256(encoded.encode()).hexdigest()):
                return {'saved': True}
            if prior['result'] is not None:
                timeout = READ_ONLY_TIMEOUTS.get(json.loads(prior['payload']).get('action'))
                if timeout and json.loads(prior['result']).get('error') == timeout:
                    return {'saved': True, 'late_read_only_result': True}
            if prior['result'] is not None and prior['result'] != encoded:
                raise PermissionError('不能覆盖已经确认的回执')
            self.db.execute('UPDATE commands SET result=? WHERE id=?', (encoded, identifier))
            return {'saved': True}
        if operation == 'recover':
            identifier = body.get('id')
            if not isinstance(identifier, str) or not re.fullmatch('[a-f0-9]{32}', identifier):
                raise ValueError('无效操作编号')
            row = self._one('SELECT * FROM commands WHERE id=?', (identifier,))
            if row is None:
                # A submission may still be in transit. Persist a tombstone
                # before reporting non-execution, so that a late submission
                # cannot subsequently execute under the same identifier.
                receipt = {'ok': False, 'request_id': identifier, 'input_attempted': False,
                           'error': 'REQUEST_NOT_ACCEPTED_CANCELLED'}
                encoded = json.dumps(receipt, sort_keys=True, separators=(',', ':'))
                self.db.execute('INSERT INTO commands VALUES(?,?,?,?,?,0,?,?)',
                                (identifier, me, '', '{}', now, encoded, now))
                return {'id': identifier, 'state': 'complete', 'result': receipt}
            if row['caller'] != me:
                raise PermissionError('找不到此操作')
            receipt = self._expire_command(row, now) if row['result'] is None else json.loads(row['result'])
            if receipt is not None:
                return {'id': identifier, 'state': 'complete', 'result': receipt}
            return {'id': identifier, 'state': 'unconfirmed', 'result': None}
        raise ValueError('不支持的请求')
