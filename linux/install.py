#!/usr/bin/env python3
"""Install a verified local Linux preview for the current desktop user."""
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import tempfile
import zipfile


DESKTOP_ID = 'local.linko.remote.desktop'
REQUIRED_FILES = {'run.sh', 'code/lanpilot/peer_client.py', 'code/peer-endpoint.json', 'code/ui/index.html'}


def read_payload(root, manifest):
    if not isinstance(manifest, dict) or not REQUIRED_FILES.issubset(manifest):
        raise ValueError('安装包缺少必要文件')
    files = {}
    for name, checksum in manifest.items():
        path = PurePosixPath(name)
        if (path.is_absolute() or '..' in path.parts or path.as_posix() != name
                or (name != 'run.sh' and (len(path.parts) < 2 or path.parts[0] != 'code'))):
            raise ValueError('安装包文件路径无效')
        source = root / name
        if not source.is_file() or (isinstance(source, Path) and source.resolve() != source):
            raise ValueError('安装包文件缺失或为符号链接：' + name)
        content = source.read_bytes()
        if hashlib.sha256(content).hexdigest() != checksum:
            raise ValueError('安装包文件校验未通过：' + name)
        files[path] = content
    return files


def desktop_entry(launcher):
    # Desktop-entry string escapes are applied before Exec argument quoting.
    arg = re.sub(r'(["`$\\])', r'\\\1', str(launcher)).replace('\\', '\\\\')
    arg = arg.replace('%', '%%').replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')
    return ('[Desktop Entry]\nType=Application\nName=邻控\nName[en]=Linko Remote\n'
            f'Exec=/bin/sh "{arg}"\nIcon=computer\nTerminal=false\nCategories=Network;\n')


def install(package, data_home):
    manifest_bytes = (package / 'checksums.json').read_bytes()
    manifest = json.loads(manifest_bytes)
    files = read_payload(package, manifest)
    base = data_home / 'linko-remote'
    releases = base / 'releases'
    menu = data_home / 'applications'
    releases.mkdir(mode=0o700, parents=True, exist_ok=True)
    menu.mkdir(parents=True, exist_ok=True)
    release = releases / hashlib.sha256(manifest_bytes).hexdigest()
    launcher = base / 'current/run.sh'
    with (base / 'install.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with tempfile.TemporaryDirectory(prefix='.install-', dir=base) as work:
            stage = Path(work)
            if not release.exists():
                package_stage = stage / 'app'
                for name, content in files.items():
                    target = package_stage / name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(content)
                (package_stage / 'run.sh').chmod(0o755)
                package_stage.rename(release)
            else:
                read_payload(release, manifest)
            # A running service continues reading its old, immutable code path.
            pointer = stage / 'current'
            pointer.symlink_to(release)
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=menu,
                                             prefix='.linko-', delete=False) as entry:
                entry.write(desktop_entry(launcher))
                entry_path = Path(entry.name)
            try:
                os.replace(pointer, base / 'current')
                os.replace(entry_path, menu / DESKTOP_ID)
            finally:
                entry_path.unlink(missing_ok=True)
    return menu / DESKTOP_ID


def main():
    if sys.version_info < (3, 9):
        raise ValueError('邻控需要 Python 3.9 或更新版本')
    data_home = Path(os.environ.get('XDG_DATA_HOME', ''))
    if not data_home.is_absolute():
        data_home = Path.home() / '.local/share'
    package = Path(__file__).resolve().parent
    if package.is_file():
        with zipfile.ZipFile(package) as archive:
            install(zipfile.Path(archive, 'LinkoRemote-Linux/'), data_home.resolve())
    else:
        install(package, data_home.resolve())
    print('邻控已安装。请从应用菜单打开。')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        sys.exit(str(error))
