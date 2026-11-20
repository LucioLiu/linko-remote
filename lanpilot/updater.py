"""User-approved updates from this project's HTTPS GitHub releases only.

Checking and downloading never stop a session. Installation is a separate
local action: verify the archive again, wait for the old process, then invoke
the platform installer. Profiles and old protocol pairings are never copied.
"""
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit
import urllib.request
import uuid
import unicodedata
import zipfile

from .version import VERSION

REPOSITORY = 'LucioLiu/linko-remote'
API_URL = 'https://api.github.com/repos/' + REPOSITORY + '/releases?per_page=20'
HOSTS = {'api.github.com', 'github.com', 'release-assets.githubusercontent.com',
         'objects.githubusercontent.com'}
MAX_ARCHIVE = 150_000_000
MAX_EXTRACTED = 350_000_000


def version_key(value):
    match = re.fullmatch(r'v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-(preview|beta|rc)\.([1-9]\d*))?', value or '')
    if not match:
        raise ValueError('无法识别版本号')
    major, minor, patch, stage, number = match.groups()
    return (int(major), int(minor), int(patch),
            {'preview': 0, 'beta': 1, 'rc': 2, None: 3}[stage], int(number or 0))


def check_url(url):
    value = urlsplit(url)
    if value.scheme != 'https' or value.hostname not in HOSTS or value.username or value.password or value.port not in (None, 443):
        raise ValueError('更新地址不是可信的 GitHub HTTPS 地址')
    return url


class HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        check_url(newurl)
        return super().redirect_request(request, fp, code, message, headers, newurl)


def open_url(url):
    check_url(url)
    request = urllib.request.Request(url, headers={'Accept': 'application/vnd.github+json',
                                     'User-Agent': 'LinkoRemote/' + VERSION})
    return urllib.request.build_opener(HTTPSRedirect()).open(request, timeout=20)


def asset_name(version, system, machine):
    if system == 'windows' and machine.lower() in ('amd64', 'x86_64'):
        return 'LinkoRemote-Windows-' + version + '.zip'
    if system == 'macos' and machine.lower() in ('arm64', 'aarch64'):
        return 'LinkoRemote-macOS-arm64-' + version + '.zip'
    raise ValueError('当前系统架构尚无可自动安装的更新包')


def select_release(releases, current, system, machine):
    current_key = version_key(current)
    candidates = []
    for release in releases:
        if release.get('draft') or (release.get('prerelease') and '-' not in current):
            continue
        tag = release.get('tag_name', '')
        try:
            key = version_key(tag)
        except (ValueError, TypeError):
            continue
        if key <= current_key:
            continue
        version = tag.removeprefix('v') if hasattr(str, 'removeprefix') else tag.lstrip('v')
        name = asset_name(version, system, machine)
        expected_url = 'https://github.com/' + REPOSITORY + '/releases/download/v' + version + '/' + name
        for asset in release.get('assets', []):
            digest = asset.get('digest', '')
            if asset.get('name') != name:
                continue
            if asset.get('browser_download_url') != expected_url or not re.fullmatch(r'sha256:[a-f0-9]{64}', digest or ''):
                raise ValueError('新版缺少可信的下载地址或 SHA-256 校验值')
            size = asset.get('size')
            if type(size) is not int or not 0 < size <= MAX_ARCHIVE:
                raise ValueError('更新包大小无效')
            candidates.append((key, {'version': version, 'name': name, 'url': expected_url,
                               'sha256': digest[7:], 'size': size,
                               'notes': str(release.get('body') or '')[:20000]}))
    return max(candidates, key=lambda x: x[0])[1] if candidates else None


def safe_extract(archive, destination, system):
    """Only the expected product root; no traversal, drives, links or duplicates."""
    root = 'LinkoRemote-Unified' if system == 'windows' else 'LinkoRemote.app'
    destination = Path(destination)
    with zipfile.ZipFile(archive) as zipped:
        seen, total = set(), 0
        for entry in zipped.infolist():
            name = entry.filename
            path = PurePosixPath(name)
            mode = entry.external_attr >> 16
            parts = name.rstrip('/').split('/')
            if ('\\' in name or ':' in name or name.startswith('/') or '..' in path.parts
                    or not path.parts or path.parts[0] != root or stat.S_ISLNK(mode)
                    or any(not part or part in ('.', '..') or part.rstrip(' .') != part for part in parts)):
                raise ValueError('更新包包含不安全的路径或链接')
            folded = unicodedata.normalize('NFC', name).casefold()
            if folded in seen:
                raise ValueError('更新包包含重复文件')
            seen.add(folded)
            total += entry.file_size
            if total > MAX_EXTRACTED or len(seen) > 10000:
                raise ValueError('更新包解压大小超出限制')
        for entry in zipped.infolist():
            path = destination.joinpath(*PurePosixPath(entry.filename).parts)
            if entry.is_dir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with zipped.open(entry) as source, path.open('wb') as target:
                    shutil.copyfileobj(source, target)
                path.chmod((entry.external_attr >> 16) & 0o777 or 0o644)
    return destination / root


class UpdateManager:
    def __init__(self, home, enabled=lambda: True, current=VERSION, system=None,
                 machine=None, opener=open_url):
        self.home = Path(home)
        self.directory = self.home / 'updates'
        self.enabled = enabled
        self.current = current
        self.system = system or ('macos' if sys.platform == 'darwin' else 'windows')
        self.machine = machine or platform.machine()
        self.opener = opener
        self.lock = threading.RLock()
        self.busy = False
        self.info = None
        self.archive = None
        self.stop = threading.Event()
        self.view = {'status': 'idle', 'current': current, 'latest': None, 'progress': 0,
                     'message': '启动后自动检查更新，也可以手动检查。', 'checked_at': None, 'notes': ''}
        result = self.directory / 'last-result.json'
        if result.exists():
            try:
                value = json.loads(result.read_text())
                self.view['last_result'] = {k: value.get(k) for k in ('ok', 'version', 'message')}
            except (ValueError, OSError):
                pass

    def snapshot(self):
        with self.lock:
            return dict(self.view, auto_check=bool(self.enabled()))

    def _set(self, **value):
        with self.lock:
            self.view.update(value)

    def start(self):
        def pump():
            if self.stop.wait(8):
                return
            while not self.stop.is_set():
                if self.enabled():
                    self.begin('check')
                if self.stop.wait(4 * 3600):
                    return
        threading.Thread(target=pump, daemon=True).start()

    def begin(self, operation):
        with self.lock:
            if self.busy or self.view['status'] == 'installing':
                return self.snapshot()
            if operation not in ('check', 'download'):
                raise ValueError('无效更新操作')
            self.busy = True
            self.view.update(status='checking' if operation == 'check' else 'downloading',
                             message='正在检查 GitHub 新版本…' if operation == 'check' else '正在下载更新…')
        def work():
            try:
                self.check() if operation == 'check' else self.download()
            except Exception as error:
                self._set(status='error', message='更新未完成：' + str(error)[:180])
            finally:
                with self.lock:
                    self.busy = False
        threading.Thread(target=work, daemon=True).start()
        return self.snapshot()

    def check(self):
        with self.opener(API_URL) as response:
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError('版本信息过大')
        releases = json.loads(raw)
        if not isinstance(releases, list):
            raise ValueError('暂时无法读取 GitHub 版本信息，请稍后重试')
        info = select_release(releases, self.current, self.system, self.machine)
        with self.lock:
            self.info, self.archive = info, None
            self.view.update(status='available' if info else 'current', latest=info['version'] if info else None,
                             checked_at=time.time(), notes=info['notes'] if info else '', progress=0,
                             message='发现新版本 ' + info['version'] if info else '当前已是最新版本')

    def download(self):
        with self.lock:
            if self.info is None:
                raise ValueError('请先检查新版本')
            info = dict(self.info)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self.directory / info['name']
        partial = path.with_suffix('.zip.part')
        digest, received = hashlib.sha256(), 0
        try:
            with self.opener(info['url']) as response, partial.open('wb') as stream:
                while True:
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > info['size']:
                        raise ValueError('更新包大小与发布记录不一致')
                    stream.write(chunk)
                    digest.update(chunk)
                    self._set(progress=int(received * 100 / info['size']))
            if received != info['size'] or digest.hexdigest() != info['sha256']:
                raise ValueError('更新包 SHA-256 校验失败，未安装')
            os.replace(partial, path)
            with self.lock:
                self.archive = path
                self.view.update(status='ready', progress=100, message='下载及校验完成，可安装并重新启动')
        finally:
            partial.unlink(missing_ok=True)

    def prepare_install(self):
        with self.lock:
            if self.busy or not self.info or self.view['status'] != 'ready' or not self.archive:
                raise ValueError('请先下载并校验更新')
            info, archive = dict(self.info), self.archive
            if hashlib.sha256(archive.read_bytes()).hexdigest() != info['sha256']:
                raise ValueError('下载后的更新包被修改，请重新下载')
            app = os.environ.get('LINKO_APP_PATH', '')
            app_pid = int(os.environ.get('LINKO_APP_PID', '0'))
            if self.system == 'macos' and (not app or not Path(app).is_dir() or app_pid <= 0):
                raise ValueError('请从已安装的邻控应用内安装更新')
            if self.system == 'macos':
                # Keep the update worker outside the bundle it will replace.
                helper = self.directory / ('helper-' + uuid.uuid4().hex)
                shutil.copytree(Path(sys.executable).parent, helper, symlinks=False)
                command = [str(helper / Path(sys.executable).name), '--apply-update']
            else:
                command = [sys.executable, '-m', 'lanpilot.updater', '--apply-update']
            spec = self.directory / ('install-' + uuid.uuid4().hex + '.json')
            value = dict(info, archive=str(archive), home=str(self.home), system=self.system,
                         parent_pid=os.getpid(), app_pid=app_pid, app=app,
                         previous_python=sys.executable,
                         previous_code=str(Path(__file__).resolve().parents[1]))
            fd = os.open(spec, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump(value, stream)
            options = {'start_new_session': True} if os.name != 'nt' else {'creationflags': 0x08000000 | 0x00000200}
            subprocess.Popen(command + [str(spec)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, **options)
            self.view.update(status='installing', message='正在安装更新并重新启动，请稍候…')
            return {'installing': True}


def wait_for_exit(pid, timeout=50):
    if type(pid) is not int or pid <= 0:
        raise ValueError('旧进程身份无效')
    if os.name == 'nt':
        # os.kill(pid, 0) terminates a process on Windows. Wait on a handle;
        # never signal a process while merely checking whether it has exited.
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x100000, False, pid)
        if not handle:
            if ctypes.get_last_error() == 87:
                return
            raise OSError('无法核对旧邻控进程，已保留安装')
        try:
            if kernel.WaitForSingleObject(handle, int(timeout * 1000)) != 0:
                raise TimeoutError('旧邻控尚未退出，已保留当前安装')
        finally:
            kernel.CloseHandle(handle)
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.25)
    raise TimeoutError('旧邻控尚未退出，已保留当前安装')


def replace_mac_app(source, target):
    """Replace only the app bundle, retaining a recoverable previous bundle."""
    import plistlib
    target, source = Path(target), Path(source)
    for app in (source, target):
        info = plistlib.loads((app / 'Contents/Info.plist').read_bytes())
        if info.get('CFBundleIdentifier') != 'local.linko.remote.unified.preview':
            raise ValueError('应用身份不匹配，未替换安装')
    subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(source)], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    staged = target.with_name('.LinkoRemote-staged-' + uuid.uuid4().hex + '.app')
    backup = target.with_name('.LinkoRemote-previous-' + uuid.uuid4().hex + '.app')
    shutil.copytree(source, staged, symlinks=False)
    moved = False
    try:
        os.replace(target, backup)
        moved = True
        os.replace(staged, target)
    except BaseException:
        if moved:
            os.replace(backup, target)
        if staged.exists():
            shutil.rmtree(staged)
        raise
    return backup


def local_state(home):
    value = json.loads((Path(home) / 'shell-session.json').read_text())
    address = urlsplit(value['url'])
    if address.scheme != 'http' or address.hostname != '127.0.0.1' or not address.port or not re.fullmatch('[A-Za-z0-9_-]{40,90}', address.fragment):
        raise ValueError('本地会话无效')
    connection = http.client.HTTPConnection(address.hostname, address.port, timeout=2)
    try:
        connection.request('POST', '/api/state', '{}', {'X-Linko-Session': address.fragment})
        response = connection.getresponse()
        if response.status != 200:
            raise ValueError('新版尚未就绪')
        return json.loads(response.read())['value']
    finally:
        connection.close()


def stop_updated_client(home, version):
    """Stop only an authenticated local instance of the attempted new version."""
    state = local_state(home)
    if state['version'] != version:
        raise ValueError('当前运行实例与待恢复版本不符')
    session = json.loads((Path(home) / 'shell-session.json').read_text())
    address = urlsplit(session['url'])
    connection = http.client.HTTPConnection('127.0.0.1', address.port, timeout=3)
    try:
        connection.request('POST', '/api/shutdown', '{}', {'X-Linko-Session': address.fragment})
        connection.getresponse().read()
    finally:
        connection.close()
    wait_for_exit(session['pid'])
    if os.name != 'nt' and session.get('app_pid'):
        pid = session['app_pid']
        command = subprocess.check_output(['/bin/ps', '-p', str(pid), '-o', 'comm='], text=True).strip()
        if command.endswith('/Contents/MacOS/LinkoRemote'):
            os.kill(pid, signal.SIGTERM)
            wait_for_exit(pid)


def restore_windows_launcher(spec):
    """Restore only this preview's two shortcuts and start its existing engine."""
    import base64
    python, code = Path(spec['previous_python']), Path(spec['previous_code'])
    if not python.is_file() or not code.is_dir():
        raise ValueError('上一版运行环境不存在')
    quote = lambda value: "'" + str(value).replace("'", "''") + "'"
    script = "$shell=New-Object -ComObject WScript.Shell;"
    script += "foreach($folder in @('Desktop','Programs')){"
    script += "$path=Join-Path ([Environment]::GetFolderPath($folder)) '邻控.lnk';"
    script += "$link=$shell.CreateShortcut($path);$link.TargetPath=" + quote(python) + ";"
    script += "$link.Arguments='-m lanpilot.peer_client --open-browser';$link.WorkingDirectory=" + quote(code) + ";$link.Save()}"
    subprocess.run(['powershell.exe', '-NoProfile', '-EncodedCommand',
                    base64.b64encode(script.encode('utf-16le')).decode()], check=True)
    subprocess.Popen([str(python), '-m', 'lanpilot.peer_client', '--open-browser'], cwd=code,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def apply_update(spec_path):
    spec_path = Path(spec_path)
    spec = json.loads(spec_path.read_text())
    home = Path(spec['home'])
    result = {'ok': False, 'version': spec['version'], 'message': '更新未完成，原配置已保留。'}
    backup = None
    stopped = False
    try:
        if spec_path.parent.resolve() != (home / 'updates').resolve():
            raise ValueError('更新任务目录无效')
        if version_key(spec['version']) < version_key(VERSION):
            raise ValueError('禁止自动安装较旧版本')
        archive = Path(spec['archive'])
        if archive.parent.resolve() != spec_path.parent.resolve() or hashlib.sha256(archive.read_bytes()).hexdigest() != spec['sha256']:
            raise ValueError('更新包校验失败')
        wait_for_exit(spec['parent_pid'])
        if spec.get('app_pid'):
            wait_for_exit(spec['app_pid'])
        stopped = True
        with tempfile.TemporaryDirectory(prefix='linko-update-', dir=spec_path.parent) as temporary:
            payload = safe_extract(archive, temporary, spec['system'])
            if spec['system'] == 'windows':
                subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                                '-File', str(payload / 'Install-Unified.ps1')], check=True)
            else:
                backup = replace_mac_app(payload, spec['app'])
                subprocess.run(['/usr/bin/open', '-n', spec['app']], check=True)
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                try:
                    if local_state(home)['version'] == spec['version']:
                        result.update(ok=True, message='已更新至 ' + spec['version'] + '，电脑名称与配对已保留。')
                        break
                except Exception:
                    pass
                time.sleep(1)
            if not result['ok']:
                raise RuntimeError('新版启动未通过检查；旧版安装与用户配置仍保留')
    except Exception as error:
        result['message'] = '更新未完成：' + str(error)[:200]
        if backup:
            try:
                try:
                    stop_updated_client(home, spec['version'])
                except (OSError, ValueError, KeyError):
                    pass
                target = Path(spec['app'])
                failed = target.with_name('.LinkoRemote-failed-' + uuid.uuid4().hex + '.app')
                os.replace(target, failed)
                os.replace(backup, target)
                subprocess.run(['/usr/bin/open', '-n', str(target)], check=True)
                result['message'] += '；已恢复上一版应用。'
            except Exception:
                result['message'] += '；上一版应用保留，可恢复。'
        elif stopped and spec.get('system') == 'windows' and spec.get('previous_python'):
            try:
                try:
                    stop_updated_client(home, spec['version'])
                except (OSError, ValueError, KeyError):
                    pass
                restore_windows_launcher(spec)
                result['message'] += '；已恢复上一版启动入口。'
            except Exception:
                result['message'] += '；上一版安装与用户配置仍保留。'
        elif stopped and spec.get('system') == 'macos' and spec.get('app'):
            try:
                subprocess.run(['/usr/bin/open', '-n', spec['app']], check=True)
            except Exception:
                pass
    finally:
        directory = home / 'updates'
        directory.mkdir(parents=True, exist_ok=True)
        temporary = directory / 'last-result.json.tmp'
        temporary.write_text(json.dumps(result, ensure_ascii=False))
        os.replace(temporary, directory / 'last-result.json')
        spec_path.unlink(missing_ok=True)
    return result


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply-update', type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(0 if apply_update(args.apply_update)['ok'] else 1)


if __name__ == '__main__':
    main()
