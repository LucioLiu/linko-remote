"""Build the shared UI and client engine for macOS, Windows and Linux.

Windows carries a private, checksum-pinned CPython runtime. It does not alter
PATH, install a global Python, or touch the 0.7 pairing/profile.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from .version import VERSION, TAG

ROOT=Path(__file__).resolve().parents[1]
WINDOWS_AVC = 'windows/video/lanpilot-avc.exe'
MACOS_MINIMUM = '14.0'


# Release payloads are intentionally enumerated. Never copy the developer's
# working directories, local profiles, logs, fixtures, or old enrollment kits.
CLIENT_FILES = (
    'desktop.py', 'lanpilot/__init__.py', 'lanpilot/peer_client.py',
    'lanpilot/peer_hub.py', 'lanpilot/updater.py', 'lanpilot/version.py',
    'lanpilot/live.py', 'lanpilot/live_agent.py', 'lanpilot/live_view.py', 'lanpilot/live_delta.py',
    'windows/Start-Desktop.ps1', 'windows/Desktop.cs',
    'windows/Live-Desktop.ps1',
    'windows/Relay.cs', 'windows/Discovery.cs', 'windows/LocalJob.ps1',
    'windows/LocalJobHost.ps1', 'windows/LedgerReconcile.cs',
    'ui/index.html', 'ui/style.css', 'ui/app.js',
)


def copy_code(destination, endpoint, files=CLIENT_FILES, extra_files=None):
    destination.mkdir(parents=True, exist_ok=True)
    extra_files = extra_files or {}
    allowed = set(files) | set(extra_files) | {'peer-endpoint.json', 'release.json', 'native/LinkoDesktop'}
    unexpected = [p.relative_to(destination).as_posix() for p in destination.rglob('*')
                  if p.is_file() and p.relative_to(destination).as_posix() not in allowed]
    if unexpected:
        raise ValueError('Unexpected files in release code directory: ' + ', '.join(unexpected))
    sources = {name: ROOT / name for name in files}
    sources.update(extra_files)
    for name, source in sources.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    (destination / 'peer-endpoint.json').write_text(json.dumps(endpoint))
    (destination / 'release.json').write_text(json.dumps({'version': VERSION, 'tag': TAG}))


def mac(output, endpoint, engine):
    if engine is None or not (engine/'LinkoService').is_file():
        raise ValueError('A frozen desktop service is required; release apps must not depend on a system Python')
    contents=output/'Contents'
    for name in ('MacOS','Resources/code/native'):(contents/name).mkdir(parents=True,exist_ok=True)
    code=contents/'Resources/code'
    copy_code(code,endpoint)
    data={'CFBundleName':'邻控','CFBundleDisplayName':'邻控','CFBundleIdentifier':'local.linko.remote.unified.preview',
          'CFBundleVersion':VERSION.split('-')[0],'CFBundleShortVersionString':VERSION.split('-')[0],
          'LinkoReleaseVersion':VERSION,'CFBundleExecutable':'LinkoRemote','NSHighResolutionCapable':True,
          'LSMinimumSystemVersion':MACOS_MINIMUM,'NSAppTransportSecurity':{'NSAllowsLocalNetworking':True},'NSScreenCaptureUsageDescription':'允许可信电脑通过邻控查看本机屏幕。'}
    (contents/'Info.plist').write_bytes(plistlib.dumps(data))
    swiftc = ['swiftc', '-target', f'{platform.machine()}-apple-macosx{MACOS_MINIMUM}']
    subprocess.run(swiftc + [str(ROOT/'lanpilot/mac/UnifiedApp.swift'),'-o',str(contents/'MacOS/LinkoRemote'),'-framework','Cocoa','-framework','WebKit'],check=True)
    subprocess.run(swiftc + ['-parse-as-library',str(ROOT/'lanpilot/mac/PeerDesktop.swift'),'-o',str(code/'native/LinkoDesktop'),'-framework','Cocoa','-framework','ScreenCaptureKit','-framework','ApplicationServices'],check=True)
    shutil.copytree(engine,contents/'Resources/engine',dirs_exist_ok=True)
    notices = engine / 'ThirdParty'
    if not notices.is_dir():
        raise ValueError('Frozen engine must include third-party license notices')
    subprocess.run(['codesign','--force','--deep','--sign','-',str(output)],check=True)


def windows(output, endpoint, runtime, expected, avc_helper=None):
    if hashlib.sha256(runtime.read_bytes()).hexdigest()!=expected:
        raise ValueError('Embedded Python archive checksum mismatch')
    output.mkdir(parents=True,exist_ok=True)
    files = CLIENT_FILES + ('windows/video/LiveAvc.cs',)
    extra = {WINDOWS_AVC: avc_helper} if avc_helper is not None else None
    # An existing build may be repackaged as JPEG-only.
    if extra is None:
        (output / 'code' / WINDOWS_AVC).unlink(missing_ok=True)
    copy_code(output/'code', endpoint, files, extra)
    with zipfile.ZipFile(runtime) as archive:
        for entry in archive.infolist():
            if entry.filename.startswith(('/', '\\')) or '..' in Path(entry.filename).parts:
                raise ValueError('Unsafe Python archive entry')
        archive.extractall(output/'runtime')
    pth=next((output/'runtime').glob('python*._pth'))
    version=pth.stem
    pth.write_text(version+'.zip\n.\n../code\n',encoding='ascii')
    shutil.copy2(ROOT/'windows/Install-Unified.ps1',output/'Install-Unified.ps1')
    (output/'打开邻控.cmd').write_bytes(b'@echo off\r\npowershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install-Unified.ps1"\r\n')
    (output/'使用说明.txt').write_text('邻控统一客户端预览\n\n双击「打开邻控.cmd」。每台电脑都有相同界面，既可连接别人，也可允许别人连接本机。\n0.7 旧配对保留在原客户端中，本预览版不覆盖原配置。\n首次连接：在对方开启允许连接并生成连接码，粘贴到本机，对方确认后即可连接。\n',encoding='utf-8-sig')
    checks={str(path.relative_to(output)).replace('\\','/'):hashlib.sha256(path.read_bytes()).hexdigest() for path in output.rglob('*') if path.is_file() and path.name != 'checksums.json'}
    (output/'checksums.json').write_text(json.dumps(checks))


def linux(output, endpoint):
    archive_type = output.suffix.lower()
    if archive_type not in ('.zip', '.pyz'):
        raise ValueError('Linux output must be a .zip archive or a .pyz installer')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.linko-build-', dir=output.parent) as temporary:
        root = Path(temporary)
        package = root / 'LinkoRemote-Linux'
        files = tuple(name for name in CLIENT_FILES if not name.startswith('windows/'))
        copy_code(package / 'code', endpoint, files)
        for name in ('run.sh', 'install.py'):
            shutil.copy2(ROOT / 'linux' / name, package / name)
            (package / name).chmod(0o755)
        payload = [package / 'run.sh', *sorted((package / 'code').rglob('*'))]
        manifest = {path.relative_to(package).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in payload if path.is_file()}
        (package / 'checksums.json').write_text(json.dumps(manifest, sort_keys=True), encoding='utf-8')
        (package / '使用说明.txt').write_text(
            '邻控 Linux 控制端预览\n\n需要 Python 3.9 或更新版本，以及桌面环境的默认浏览器。\n'
            '解压后运行 python3 LinkoRemote-Linux/install.py，再从应用菜单打开「邻控」。\n'
            '直接运行 sh LinkoRemote-Linux/run.sh 也可打开界面。\n'
            '连接服务须识别 Linux 客户端。本预览支持主动连接，Linux 被控支持另行提供。\n',
            encoding='utf-8')
        archive_path = root / output.name
        if archive_type == '.pyz':
            archive_path.write_bytes(b'#!/usr/bin/env python3\n')
            archive_path.chmod(0o755)
        with zipfile.ZipFile(archive_path, 'a', zipfile.ZIP_DEFLATED) as archive:
            if archive_type == '.pyz':
                archive.write(package / 'install.py', '__main__.py')
            for path in sorted(package.rglob('*')):
                if path.is_file():
                    archive.write(path, path.relative_to(root).as_posix())
        os.replace(archive_path, output)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('platform',choices=['macos','windows','linux']);parser.add_argument('output',type=Path)
    parser.add_argument('--endpoint',type=Path,required=True);parser.add_argument('--runtime',type=Path);parser.add_argument('--runtime-sha256')
    parser.add_argument('--engine',type=Path)
    parser.add_argument('--avc-helper', type=Path, help='Prebuilt Windows AVC helper matching the embedded Python architecture')
    args=parser.parse_args();endpoint=json.loads(args.endpoint.read_text())
    if set(endpoint)!={'url','pin'}:raise ValueError('Endpoint metadata must contain only public URL and certificate pin')
    if args.platform=='macos':mac(args.output.resolve(),endpoint,args.engine)
    elif args.platform=='linux':linux(args.output.resolve(),endpoint)
    else:windows(args.output.resolve(),endpoint,args.runtime,args.runtime_sha256,args.avc_helper)
    print(args.output.resolve())


if __name__=='__main__':main()
