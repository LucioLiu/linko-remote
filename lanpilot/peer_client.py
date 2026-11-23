"""One cross-platform desktop client: both session initiator and desktop agent."""
import base64
import hashlib
import hmac
import http.client
import http.server
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit
from urllib.parse import parse_qs
import uuid
import webbrowser

from desktop import make_request
from .peer_hub import READ_ONLY_TIMEOUTS, display_name
from .version import VERSION
from .updater import UpdateManager
from .live_agent import LiveAgent
from .live_view import LiveView

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'ui'
MAX_BODY = 24_000_000
STATE_STALE_AFTER = 8
LEGACY_PREVIEW_ENDPOINT = {'url': 'https://bore.pub:49006',
                           'pin': 'd660698b3705ed323698955c0e36fe0e2be71d11777eee3d2e2bc1ff2dde73af'}


class InstanceLock:
    def __init__(self, home):
        self.path = Path(home) / 'app.lock'
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.file = None

    def acquire(self):
        self.file = self.path.open('a+b')
        try:
            if sys.platform == 'win32':
                import msvcrt
                self.file.seek(0)
                if not self.file.read(1):
                    self.file.write(b'0')
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self.file.close()
            self.file = None
            return False

    def close(self):
        if self.file:
            self.file.close()
            self.file = None


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(',', ':'))
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class PinnedConnection(http.client.HTTPSConnection):
    def __init__(self, host, port, pin):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        super().__init__(host, port, context=context, timeout=12)
        self.pin = pin

    def connect(self):
        super().connect()
        if not hmac.compare_digest(hashlib.sha256(self.sock.getpeercert(True)).hexdigest(), self.pin):
            self.close()
            raise RelayIdentityError('连接服务证书不匹配，已停止连接')


class HubRejection(PermissionError):
    """The relay confirmed it rejected an operation before accepting it."""


class RelayIdentityError(PermissionError):
    """No request may be sent when the relay certificate changes."""


class PinnedHub:
    def __init__(self, endpoint, credential):
        parsed = urlsplit(endpoint['url'])
        if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment or not parsed.hostname:
            raise ValueError('连接服务地址必须是 HTTPS')
        if not re.fullmatch('[a-f0-9]{64}', endpoint.get('pin', '')):
            raise ValueError('缺少连接服务的证书指纹')
        self.host, self.port, self.pin = parsed.hostname, parsed.port or 443, endpoint['pin']
        self.credential = credential
        self.lock = threading.Lock()
        self.connection = None
        self.last_response = 0

    def call(self, operation, body=None):
        # Read-only recovery may use a fresh connection after a relay closes an
        # idle TLS socket. Never retry a command submission or any input.
        for attempt in range(3):
            try:
                return self._call(operation, body)
            except (HubRejection, RelayIdentityError):
                raise
            except (OSError, http.client.HTTPException):
                if operation not in ('state', 'recover', 'live-view', 'live-agent-state', 'live-exchange', 'live-frame') or attempt == 2:
                    raise

    def _call(self, operation, body=None):
        with self.lock:
            try:
                # The gateway bounds idle sockets to 30 seconds. Retire a
                # locally idle socket before writing, never replay after send.
                # Start each new command on a fresh connection. Its initial
                # and subsequent receipt queries can reuse that connection.
                if self.connection and (operation == 'submit' or time.monotonic() - self.last_response >= 20):
                    self.connection.close()
                    self.connection = None
                if self.connection is None or self.connection.sock is None:
                    self._connect()
                payload = json.dumps(body or {}, ensure_ascii=False, separators=(',', ':')).encode()
                self.connection.request('POST', '/v2/' + operation, payload,
                                        {'Authorization': 'Bearer ' + self.credential, 'Content-Type': 'application/json'})
                response = self.connection.getresponse()
                raw = response.read(MAX_BODY + 1)
                if len(raw) > MAX_BODY:
                    raise ValueError('连接服务响应过大')
                answer = json.loads(raw)
                if response.status in (400, 401, 403) and answer.get('ok') is False:
                    raise HubRejection(answer.get('error', '连接请求未完成'))
                if response.status != 200 or not answer.get('ok'):
                    raise PermissionError(answer.get('error', '连接请求未完成'))
                self.last_response = time.monotonic()
                return answer['value']
            except BaseException:
                if self.connection:
                    self.connection.close()
                    self.connection = None
                # A lost response never causes an action to be submitted again.
                raise

    def _connect(self):
        # Establish TLS before handing any request bytes to the socket. A
        # failed handshake can be retried even for input; a failed write cannot.
        for attempt in range(2):
            connection = PinnedConnection(self.host, self.port, self.pin)
            try:
                connection.connect()
            except RelayIdentityError:
                connection.close()
                raise
            except OSError:
                connection.close()
                if attempt:
                    raise
            else:
                self.connection = connection
                return


class MailboxAgent:
    def __init__(self, home):
        self.home = Path(home)
        self.workspace = self.home / 'worker'
        self.workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.process = None
        self.stop_path = self.workspace / 'stop'

    def permissions(self):
        if sys.platform == 'darwin':
            helper = ROOT / 'native/LinkoDesktop'
            result = subprocess.run([str(helper), '--permissions'], capture_output=True, timeout=8)
            return json.loads(result.stdout)
        return {'screen': True, 'input': True, 'platform': 'windows'}

    def start(self):
        if self.process and self.process.poll() is None:
            if not self.stop_path.exists():
                return
            self.process.wait(timeout=5)
        self.stop_path.unlink(missing_ok=True)
        if sys.platform == 'darwin':
            permissions = self.permissions()
            if not permissions.get('screen') or not permissions.get('input'):
                raise PermissionError('请先在 macOS 系统设置中允许邻控录屏和辅助功能')
            helper = ROOT / 'native/LinkoDesktop'
            command = [str(helper), '--workspace', str(self.workspace)]
            options = {}
        else:
            command = ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                       str(ROOT / 'windows/Start-Desktop.ps1'), '-AllowInput', '-TargetGuard',
                       '-Workspace', str(self.workspace), '-StopFile', str(self.stop_path)]
            options = {'creationflags': subprocess.CREATE_NO_WINDOW}
        self.process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)

    def running(self):
        return self.process is not None and self.process.poll() is None and not self.stop_path.exists()

    def stop(self):
        self.stop_path.write_text('stop')
        # The worker checks this immediately before sending any input. Pending
        # durable results remain available to the receipt pump on next launch.

    def execute(self, command, elapsed=0):
        identifier = command['id']
        if not re.fullmatch('[a-f0-9]{32}', identifier):
            raise ValueError('无效操作编号')
        response = self.workspace / (identifier + '.response.json')
        if response.exists():
            return json.loads(response.read_text(encoding='utf-8-sig'))
        lifetime = command['valid_for_ms'] / 1000 - elapsed
        if lifetime <= 0:
            return {'ok': False, 'input_attempted': False, 'request_id': identifier, 'error': 'REQUEST_EXPIRED_IN_TRANSIT'}
        if not self.running():
            return {'ok': False, 'input_attempted': False, 'request_id': identifier, 'error': 'LOCAL_CONTROL_NOT_ENABLED'}
        request = dict(command['request'], expires=int(time.time() + min(45, lifetime)))
        path = self.workspace / (identifier + '.request.json')
        # Publish exactly once. A duplicate transport delivery cannot repeat input.
        marker = self.workspace / (identifier + '.accepted')
        try:
            fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        except FileExistsError:
            if response.exists():
                return json.loads(response.read_text(encoding='utf-8-sig'))
            return None
        atomic_json(path, request)
        return None


class DesktopClient:
    def __init__(self, home, endpoint, hub_factory=PinnedHub, agent_factory=MailboxAgent):
        self.home = Path(home)
        self.home.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.platform = 'macos' if sys.platform == 'darwin' else 'windows'
        self.path = self.home / 'client.json'
        if self.path.exists():
            self.config = json.loads(self.path.read_text())
            if self.config.get('endpoint') == LEGACY_PREVIEW_ENDPOINT and endpoint != LEGACY_PREVIEW_ENDPOINT:
                # The verified release can replace its own retired preview
                # service. Custom/self-hosted endpoints are never overwritten.
                atomic_json(self.home / 'client-before-relay-migration.json', self.config)
                self.config['endpoint'] = endpoint
                atomic_json(self.path, self.config)
        else:
            credential = secrets.token_urlsafe(32)
            # Never import the developer's or the current user's OS hostname.
            label = 'Mac 电脑' if self.platform == 'macos' else 'Windows 电脑'
            self.config = {'credential': credential,
                           'name': label + ' ' + hashlib.sha256(credential.encode()).hexdigest()[:4],
                           'endpoint': endpoint, 'ssh': []}
            atomic_json(self.path, self.config)
        self.agent_hub = hub_factory(self.config['endpoint'], self.config['credential'])
        self.ui_hub = hub_factory(self.config['endpoint'], self.config['credential'])
        self.command_hub = hub_factory(self.config['endpoint'], self.config['credential'])
        self.live_hub = hub_factory(self.config['endpoint'], self.config['credential'])
        self.live_input_hub = hub_factory(self.config['endpoint'], self.config['credential'])
        self.agent = agent_factory(self.home)
        self.stop_event = threading.Event()
        self.lock = threading.RLock()
        self.operation_lock = threading.RLock()
        self.command_lock = threading.Lock()
        self.incoming = False
        self.pause_pending = False
        self.connected = False
        self.state_seen_at = 0
        self.state_seen_wall = 0
        self.error = ''
        self.agent_error = ''
        self.state = {'self': {'name': self.config['name'], 'platform': self.platform},
                      'incoming': False, 'peers': [], 'approvals': [], 'outgoing': []}
        self.permissions = {'screen': False, 'input': False}
        self.permissions_checked_at = None
        self.pending_path = self.home / 'pending.json'
        self.pending = json.loads(self.pending_path.read_text()) if self.pending_path.exists() else {}
        self.updating = False
        self.updater = UpdateManager(self.home, lambda: self.config.get('auto_update_check', True))
        self.live_agent = LiveAgent(self, hub_factory, atomic_json)
        self.live_session = None
        self.live_view = None

    def start(self):
        threading.Thread(target=self._pump, daemon=True).start()
        self.updater.start()
        self.live_agent.start()

    def _pump(self):
        registered = False
        while not self.stop_event.is_set():
            waiting_for_worker = False
            try:
                if not registered:
                    with self.operation_lock:
                        if self.stop_event.is_set():
                            return
                        self.agent_hub.call('register', {'name': self.config['name'], 'platform': self.platform, 'live_version': 1})
                        # Clear temporary grants before restoring explicitly trusted peers.
                        self.agent_hub.call('availability', {'enabled': False})
                        if self.config.get('resume_trusted_access', True):
                            peers = self.agent_hub.call('state').get('peers', [])
                            if any(peer.get('trusted_incoming') for peer in peers):
                                try:
                                    self._start_agent()
                                except (OSError, ValueError, subprocess.SubprocessError):
                                    # This computer can still reach the relay and make outgoing connections.
                                    pass
                                else:
                                    self._enable_relay_availability(self.agent_hub)
                    registered = True
                self._reconcile_incoming()
                waiting_for_worker = self._sync_commands()
                if not self._state_is_current(2):
                    started_wall = time.time()
                    started = time.monotonic()
                    value = self.agent_hub.call('state')
                    with self.lock:
                        self.state = value
                        self.state_seen_at = started
                        self.state_seen_wall = started_wall
                        self.connected = True
                        self.error = ''
            except Exception as error:
                with self.lock:
                    self.connected = False
                    self.error = str(error)[:250]
            self._refresh_permissions()
            self.stop_event.wait(.15 if waiting_for_worker else 1)

    def _state_is_current(self, max_age):
        # Uptime can stop during sleep. Date the snapshot from the start of
        # the request so a reply delayed across sleep cannot refresh it.
        wall_age = time.time() - self.state_seen_wall
        return self.connected and 0 <= wall_age < max_age and time.monotonic() - self.state_seen_at < max_age

    def _refresh_permissions(self):
        now = time.monotonic()
        if self.permissions_checked_at is not None and now - self.permissions_checked_at < 5:
            return
        self.permissions_checked_at = now
        try:
            self.permissions = self.agent.permissions()
        except (OSError, ValueError, subprocess.SubprocessError):
            self.permissions = {'screen': False, 'input': False}

    def _start_agent(self):
        try:
            self.agent.start()
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            self.agent_error = str(error)[:250]
            raise
        self.agent_error = ''

    def _stop_incoming(self):
        self.incoming = False
        self.agent.stop()
        self.live_agent.expire()

    def _enable_relay_availability(self, hub):
        try:
            value = hub.call('availability', {'enabled': True})
        except Exception:
            # The relay may have accepted the change before the reply was lost.
            self.pause_pending = True
            self._stop_incoming()
            raise
        self.incoming = True
        self.pause_pending = False
        return value

    def _reconcile_incoming(self):
        if not self.pause_pending and (not self.incoming or self.agent.running()):
            return
        with self.operation_lock:
            if self.incoming and not self.agent.running():
                self.agent_error = '本机接收进程已停止，请重新开启允许连接'
                self.pause_pending = True
                self._stop_incoming()
            if self.pause_pending:
                self.agent_hub.call('availability', {'enabled': False})
                self.pause_pending = False

    def _save_resume_preference(self, enabled):
        with self.lock:
            atomic_json(self.path, dict(self.config, resume_trusted_access=enabled))
            self.config['resume_trusted_access'] = enabled

    def _sync_commands(self):
        workspace = self.agent.workspace
        outbox = workspace / 'hub-outbox.json'
        delivery = workspace / 'delivery.json'
        if not outbox.exists() and not self._prepare_receipt(outbox):
            return delivery.exists() and time.time() - delivery.stat().st_mtime < 45
        receipt = json.loads(outbox.read_text(encoding='utf-8-sig'))
        self.agent_hub.call('result', receipt)
        # An interruption between writing the outbox and unlinking delivery
        # can leave both files. Clear only the delivery this receipt confirms.
        if delivery.exists() and json.loads(delivery.read_text())['id'] == receipt['id']:
            delivery.unlink()
        outbox.unlink()
        return False

    def _prepare_receipt(self, outbox):
        workspace = self.agent.workspace
        delivery = workspace / 'delivery.json'
        if delivery.exists():
            command = json.loads(delivery.read_text())
            identifier = command['id']
            response = workspace / (identifier + '.response.json')
            if response.exists():
                result = json.loads(response.read_text(encoding='utf-8-sig'))
            else:
                error = READ_ONLY_TIMEOUTS.get(command['request']['action'])
                expires = delivery.stat().st_mtime + command['valid_for_ms'] / 1000
                if not error or time.time() < expires:
                    return False
                # A worker can stop after claiming a snapshot and leave no
                # response. Free the mailbox without executing it again.
                result = {'request_id': identifier, 'ok': False, 'input_attempted': False, 'error': error}
        else:
            started_wall = time.time()
            started = time.monotonic()
            command = self.agent_hub.call('poll')
            if not command:
                return False
            identifier = command['id']
            atomic_json(delivery, command)
            elapsed = max(time.monotonic() - started, time.time() - started_wall)
            result = self.agent.execute(command, elapsed)
        if result is not None:
            atomic_json(outbox, {'id': identifier, 'result': result})
            delivery.unlink()
            return True
        return False

    def local_state(self):
        with self.lock:
            value = dict(self.state)
            connected = self._state_is_current(STATE_STALE_AFTER)
            value.update(version=VERSION, connected=connected, error=self.error,
                         incoming=self.incoming, pause_pending=self.pause_pending, agent_error=self.agent_error,
                         permissions=self.permissions, ssh=self.config['ssh'], pending=dict(self.pending))
        value['updates'] = self.updater.snapshot()
        value['live_error'] = self.live_agent.error
        view = self.live_view
        value['live'] = view.snapshot(touch=False) if view else None
        return value

    def api(self, operation, body):
        if operation == 'state':
            return self.local_state()
        if operation == 'live-view':
            if not self.live_view or body.get('id') != self.live_view.id:
                raise ValueError('连续桌面会话已结束')
            return self.live_view.snapshot()
        if operation in ('remote', 'recover'):
            # Keep submission and recovery ordered, but a slow receipt must
            # not hold the lock or transport used to pause incoming control.
            with self.command_lock:
                with self.operation_lock:
                    if self.updating or self.stop_event.is_set():
                        raise ValueError('邻控正在退出或更新，请稍后再试')
                    if operation == 'remote':
                        submission = self._prepare_submission(body)
                if operation == 'remote':
                    return self._submit_remote(submission)
                return self.recover(body.get('id'))
        with self.operation_lock:
            return self._api(operation, body)

    def _api(self, operation, body):
        if operation in ('live-open', 'live-input', 'live-close'):
            if self.updating:
                raise ValueError('正在更新，暂不能连接')
            if operation == 'live-open' and self.pending.get(body.get('peer')):
                raise ValueError('请先查询上一操作结果')
            if operation == 'live-close':
                if self.live_view and self.live_view.id == body.get('id'): self.live_view.close()
                self.live_session = None
                try: return self.live_input_hub.call(operation, body)
                except PermissionError: return {'closed': True}
            if operation == 'live-open' and self.live_view and not self.live_view.stop.is_set():
                raise ValueError('请先关闭当前远程桌面')
            value = self.live_input_hub.call(operation, body)
            if operation == 'live-open':
                self.live_session = value['id']
                self.live_view = LiveView(self.live_hub, value['id'], self.stop_event)
                self.live_view.start()
            return value
        if operation == 'update-check':
            return self.updater.begin('check')
        if operation == 'update-download':
            return self.updater.begin('download')
        if operation == 'update-preference':
            enabled = body.get('enabled')
            if type(enabled) is not bool:
                raise ValueError('请选择是否自动检查更新')
            with self.lock:
                self.config['auto_update_check'] = enabled
                atomic_json(self.path, self.config)
            return self.updater.snapshot()
        if operation == 'update-install':
            with self.lock:
                peers = self.state.get('peers', [])
                if self.live_session or self.pending or self.incoming or any(p.get('incoming') or p.get('can_control') for p in peers):
                    raise ValueError('请先结束远程连接、确认原操作结果，并关闭允许连接，再安装更新')
                value = self.updater.prepare_install(expect_relay=self._state_is_current(5))
                self.updating = True
                return value
        if self.updating:
            raise ValueError('正在更新，暂不能发起新的操作')
        if operation == 'availability':
            enabled = body.get('enabled')
            if type(enabled) is not bool:
                raise ValueError('请明确选择是否允许连接')
            if enabled:
                self._start_agent()
                value = self._enable_relay_availability(self.ui_hub)
                try:
                    self._save_resume_preference(True)
                except OSError:
                    self.pause_pending = True
                    self._stop_incoming()
                    raise
                return value
            self.pause_pending = True
            self._stop_incoming()
            self._save_resume_preference(False)
            try:
                value = self.ui_hub.call(operation, {'enabled': False})
            except (RelayIdentityError, HubRejection):
                raise
            except (OSError, http.client.HTTPException):
                return {'incoming': False, 'sync_pending': True}
            self.pause_pending = False
            return value
        if operation == 'name':
            name = display_name(body.get('name'))
            value = self.ui_hub.call(operation, {'name': name})
            self.config['name'] = name
            atomic_json(self.path, self.config)
            return value
        if operation in ('code', 'connect', 'access', 'approve', 'disconnect'):
            if operation == 'connect':
                code = body.get('code', '')
                if isinstance(code, str) and code.startswith('linko2:'):
                    try:
                        encoded = code[7:]
                        if len(encoded) > 2000:
                            raise ValueError()
                        decoded = json.loads(base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4)))
                        if decoded['v'] != 2 or decoded['url'].rstrip('/') != self.config['endpoint']['url'].rstrip('/') or not hmac.compare_digest(decoded['pin'], self.config['endpoint']['pin']):
                            raise ValueError()
                        code = decoded['code']
                    except (KeyError, ValueError, TypeError):
                        raise ValueError('连接码无效，或两台电脑使用了不同的连接服务')
                elif isinstance(code, str):
                    code = re.sub(r'[\s-]', '', code)
                body = {'code': code}
            if operation == 'approve' and body.get('allow'):
                self.agent.start()
            value = self.ui_hub.call(operation, body)
            if operation == 'code':
                encoded = dict(v=2, **self.config['endpoint'], code=value['code'])
                value['connection'] = 'linko2:' + base64.urlsafe_b64encode(json.dumps(encoded).encode()).decode().rstrip('=')
            return value
        if operation == 'ssh-save':
            return self.save_ssh(body)
        if operation == 'ssh-open':
            return self.open_ssh(body.get('id'))
        if operation == 'permissions':
            if sys.platform == 'darwin':
                pages = {'screen': 'Privacy_ScreenCapture', 'input': 'Privacy_Accessibility'}
                if body.get('kind') not in pages:
                    raise ValueError('权限类型无效')
                helper = ROOT / 'native/LinkoDesktop'
                subprocess.Popen([str(helper), '--request-' + body['kind']], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                subprocess.run(['/usr/bin/open', 'x-apple.systempreferences:com.apple.preference.security?' + pages[body['kind']]], check=True)
            return {'opened': sys.platform == 'darwin'}
        raise ValueError('不支持的操作')

    def _prepare_submission(self, body):
        peer = body.get('peer')
        raw = body.get('request', {})
        with self.lock:
            if peer in self.pending:
                raise ValueError('此电脑上一操作未确认，请先查询原结果')
            request = make_request(raw.get('action'), **{k: v for k, v in raw.items() if k not in ('action', 'expires')})
            request.pop('expires')
            identifier = uuid.uuid4().hex
            pending = dict(self.pending)
            pending[peer] = identifier
            atomic_json(self.pending_path, pending)
            self.pending = pending
        return {'peer': peer, 'id': identifier, 'request': request}

    def _submit_remote(self, submission):
        identifier = submission['id']
        try:
            self.command_hub.call('submit', submission)
        except (HubRejection, RelayIdentityError):
            self.clear_pending(identifier)
            raise
        except Exception:
            return {'id': identifier, 'state': 'unconfirmed', 'result': None}
        try:
            return self.recover(identifier)
        except (RelayIdentityError, HubRejection):
            # The relay has already accepted the submission. Keep its number.
            raise
        except (OSError, http.client.HTTPException):
            return {'id': identifier, 'state': 'unconfirmed', 'result': None}

    def recover(self, identifier):
        if not isinstance(identifier, str) or not re.fullmatch('[a-f0-9]{32}', identifier):
            raise ValueError('无效操作编号')
        result = self.command_hub.call('recover', {'id': identifier})
        if result['state'] == 'complete':
            self.clear_pending(identifier)
        return result

    def clear_pending(self, identifier):
        with self.lock:
            pending = {key: value for key, value in self.pending.items() if value != identifier}
            if pending != self.pending:
                atomic_json(self.pending_path, pending)
                self.pending = pending

    def save_ssh(self, body):
        name = display_name(body.get('name'))
        host, user = body.get('host', '').strip(), body.get('user', '').strip()
        port = int(body.get('port') or 22)
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.:-]{0,252}', host) or (user and not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}', user)) or not 1 <= port <= 65535:
            raise ValueError('SSH 地址、用户名或端口无效')
        item = dict(id=uuid.uuid4().hex[:12], name=name, host=host, user=user, port=port)
        with self.lock:
            self.config['ssh'].append(item)
            atomic_json(self.path, self.config)
        return item

    def open_ssh(self, identifier):
        item = next((x for x in self.config['ssh'] if x['id'] == identifier), None)
        if item is None:
            raise ValueError('找不到 SSH 连接')
        args = ['ssh', '-p', str(item['port'])]
        if item['user']:
            args += ['-l', item['user']]
        args += ['--', item['host']]
        if sys.platform == 'darwin':
            path = self.home / ('ssh-' + identifier + '.command')
            path.write_text('#!/bin/sh\nexec ' + shlex.join(args) + '\n')
            path.chmod(0o700)
            subprocess.Popen(['/usr/bin/open', '-a', 'Terminal', str(path)])
        else:
            subprocess.Popen(['cmd.exe', '/d', '/c', 'start', '', 'ssh.exe', *args[1:]])
        return {'opened': True}

    def close(self):
        self.stop_event.set()
        if self.live_view: self.live_view.close()
        self.updater.stop.set()
        with self.operation_lock:
            self._stop_incoming()
            try:
                self.ui_hub.call('availability', {'enabled': False})
            except Exception:
                pass


class LocalHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def respond(self, status, body, mime='application/json; charset=utf-8'):
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(body)

    def valid_host(self):
        return self.headers.get_all('Host', []) == [self.server.authority]

    def do_GET(self):
        if not self.valid_host():
            return self.respond(403, b'{}')
        if self.path.startswith('/live-feed/'):
            return self.live_feed()
        files = {'/': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'application/javascript; charset=utf-8'), '/style.css': ('style.css', 'text/css; charset=utf-8')}
        if self.path not in files:
            return self.respond(404, b'{}')
        name, mime = files[self.path]
        return self.respond(200, (ASSETS / name).read_bytes(), mime)

    def live_feed(self):
        parsed = urlsplit(self.path)
        identifier = parsed.path.removeprefix('/live-feed/')
        nonce = parse_qs(parsed.query).get('session', [''])
        view = getattr(self.server.client, 'live_view', None)
        if (not re.fullmatch('[a-f0-9]{32}', identifier) or len(nonce) != 1
                or not hmac.compare_digest(nonce[0], self.server.nonce)
                or self.headers.get('Origin') not in (None, 'http://' + self.server.authority)
                or not view or view.id != identifier):
            return self.respond(403, b'{}')
        self.close_connection = True
        self.connection.settimeout(3)
        self.send_response(200)
        self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=linko-frame')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(b'--linko-frame\r\n')
        sequence = None
        try:
            while not view.stop.is_set() and not self.server.client.stop_event.is_set():
                with view.changed:
                    frame = view.value.get('frame')
                    if not frame or frame['seq'] == sequence:
                        view.changed.wait(.5)
                        continue
                    sequence = frame['seq']
                    raw = base64.b64decode(frame['jpeg_base64'], validate=True)
                # Complete the boundary immediately: image decoders must not
                # wait for the next WAN frame to finish the preceding part.
                self.wfile.write(b'Content-Type: image/jpeg\r\nContent-Length: '
                                 + str(len(raw)).encode() + b'\r\n\r\n' + raw + b'\r\n--linko-frame\r\n')
                self.wfile.flush()
        except (OSError, ValueError):
            pass

    def do_POST(self):
        if not self.valid_host() or self.headers.get('Origin') not in (None, 'http://' + self.server.authority) or not hmac.compare_digest(self.headers.get('X-Linko-Session', ''), self.server.nonce):
            self.close_connection = True
            return self.respond(403, b'{}')
        lengths = self.headers.get_all('Content-Length', [])
        if len(lengths) != 1 or not lengths[0].isdigit() or not 0 < int(lengths[0]) <= 16000 or self.headers.get('Transfer-Encoding'):
            self.close_connection = True
            return self.respond(400, b'{}')
        try:
            body = json.loads(self.rfile.read(int(lengths[0])))
            if not isinstance(body, dict) or not re.fullmatch('/api/[a-z-]+', self.path):
                raise ValueError('无效请求')
            if self.path == '/api/shutdown':
                self.respond(200, b'{"ok":true,"value":{"stopped":true}}')
                self.server.client.agent.stop()
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            value = self.server.client.api(self.path[5:], body)
            data = json.dumps({'ok': True, 'value': value}, ensure_ascii=False, separators=(',', ':')).encode()
            try:
                self.respond(200, data)
            finally:
                if self.path == '/api/update-install':
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
        except Exception as error:
            self.respond(400, json.dumps({'ok': False, 'error': str(error)[:250]}, ensure_ascii=False).encode())


def run(home, endpoint, open_browser=False):
    client = DesktopClient(home, endpoint)
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), LocalHandler)
    server.daemon_threads = True
    server.client = client
    server.authority = '127.0.0.1:' + str(server.server_port)
    server.nonce = secrets.token_urlsafe(32)
    url = 'http://' + server.authority + '/#' + server.nonce
    atomic_json(Path(home) / 'shell-session.json', {'url': url, 'pid': os.getpid(),
                'app_pid': int(os.environ.get('LINKO_APP_PID', '0'))})
    if sys.stdout is not None:
        print(json.dumps({'url': url}), flush=True)
    client.start()
    def terminate(*_):
        client.agent.stop()
        threading.Thread(target=server.shutdown, daemon=True).start()
    for event in (signal.SIGINT, signal.SIGTERM):
        signal.signal(event, terminate)
    if open_browser:
        if sys.platform == 'win32':
            edge = Path(os.environ.get('PROGRAMFILES(X86)', 'C:/Program Files (x86)')) / 'Microsoft/Edge/Application/msedge.exe'
            if edge.exists():
                subprocess.Popen([str(edge), '--app=' + url, '--user-data-dir=' + str(Path(home) / 'webview-profile')])
            else:
                webbrowser.open(url)
        else:
            webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.3)
    finally:
        client.close()
        server.server_close()


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--home', type=Path)
    parser.add_argument('--endpoint', type=Path, default=ROOT / 'peer-endpoint.json')
    parser.add_argument('--open-browser', action='store_true')
    args = parser.parse_args()
    home = args.home or ((Path(os.environ['LOCALAPPDATA']) / 'LinkoRemote') if sys.platform == 'win32' else Path.home() / 'Library/Application Support/LinkoRemote')
    endpoint = json.loads(args.endpoint.read_text())
    lock = InstanceLock(home)
    if not lock.acquire():
        session = json.loads((home / 'shell-session.json').read_text())
        url = session['url']
        parsed = urlsplit(url)
        if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port or not re.fullmatch('[A-Za-z0-9_-]{40,90}', parsed.fragment):
            raise ValueError('本机邻控会话无效')
        check = http.client.HTTPConnection('127.0.0.1', parsed.port, timeout=3)
        try:
            check.request('POST', '/api/state', '{}', {'X-Linko-Session': parsed.fragment})
            response = check.getresponse()
            if response.status != 200:
                raise ValueError('本机邻控服务尚未就绪')
            response.read()
        finally:
            check.close()
        if sys.stdout is not None:
            print(json.dumps({'url': url}), flush=True)
        if args.open_browser:
            webbrowser.open(url)
        return
    try:
        run(home, endpoint, args.open_browser)
    finally:
        lock.close()


if __name__ == '__main__':
    main()
