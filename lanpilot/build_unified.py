"""Build the same shared UI and client engine for macOS and Windows.

Windows carries a private, checksum-pinned CPython runtime. It does not alter
PATH, install a global Python, or touch the 0.7 pairing/profile.
"""
import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from .version import VERSION, TAG

ROOT=Path(__file__).resolve().parents[1]


# Release payloads are intentionally enumerated. Never copy the developer's
# working directories, local profiles, logs, fixtures, or old enrollment kits.
CLIENT_FILES = (
    'desktop.py', 'lanpilot/__init__.py', 'lanpilot/peer_client.py',
    'lanpilot/peer_hub.py', 'lanpilot/updater.py', 'lanpilot/version.py',
    'windows/Start-Desktop.ps1', 'windows/Desktop.cs',
    'windows/Relay.cs', 'windows/Discovery.cs', 'windows/LocalJob.ps1',
    'windows/LocalJobHost.ps1', 'windows/LedgerReconcile.cs',
    'ui/index.html', 'ui/style.css', 'ui/app.js',
)


def copy_code(destination, endpoint):
    destination.mkdir(parents=True, exist_ok=True)
    allowed = set(CLIENT_FILES) | {'peer-endpoint.json', 'release.json', 'native/LinkoDesktop'}
    unexpected = [p.relative_to(destination).as_posix() for p in destination.rglob('*')
                  if p.is_file() and p.relative_to(destination).as_posix() not in allowed]
    if unexpected:
        raise ValueError('Unexpected files in release code directory: ' + ', '.join(unexpected))
    for name in CLIENT_FILES:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    (destination / 'peer-endpoint.json').write_text(json.dumps(endpoint))
    (destination / 'release.json').write_text(json.dumps({'version': VERSION, 'tag': TAG}))


def mac(output, endpoint, engine):
    if engine is None or not (engine/'LinkoService').is_file():
        raise ValueError('A frozen desktop service is required; release apps must not depend on a system Python')
    contents=output/'Contents'
    for name in ('MacOS','Resources/code/native'):(contents/name).mkdir(parents=True,exist_ok=True)
    code=contents/'Resources/code'
    copy_code(code,endpoint)
    data={'CFBundleName':'邻控统一预览','CFBundleDisplayName':'邻控统一预览','CFBundleIdentifier':'local.linko.remote.unified.preview',
          'CFBundleVersion':VERSION.split('-')[0],'CFBundleShortVersionString':VERSION.split('-')[0],
          'LinkoReleaseVersion':VERSION,'CFBundleExecutable':'LinkoRemote','NSHighResolutionCapable':True,
          'LSMinimumSystemVersion':'14.0','NSAppTransportSecurity':{'NSAllowsLocalNetworking':True},'NSScreenCaptureUsageDescription':'允许可信电脑通过邻控查看本机屏幕。'}
    (contents/'Info.plist').write_bytes(plistlib.dumps(data))
    subprocess.run(['swiftc',str(ROOT/'lanpilot/mac/UnifiedApp.swift'),'-o',str(contents/'MacOS/LinkoRemote'),'-framework','Cocoa','-framework','WebKit'],check=True)
    subprocess.run(['swiftc','-parse-as-library',str(ROOT/'lanpilot/mac/PeerDesktop.swift'),'-o',str(code/'native/LinkoDesktop'),'-framework','Cocoa','-framework','ScreenCaptureKit','-framework','ApplicationServices'],check=True)
    shutil.copytree(engine,contents/'Resources/engine',dirs_exist_ok=True)
    notices = engine / 'ThirdParty'
    if not notices.is_dir():
        raise ValueError('Frozen engine must include third-party license notices')
    subprocess.run(['codesign','--force','--deep','--sign','-',str(output)],check=True)


def windows(output, endpoint, runtime, expected):
    if hashlib.sha256(runtime.read_bytes()).hexdigest()!=expected:
        raise ValueError('Embedded Python archive checksum mismatch')
    output.mkdir(parents=True,exist_ok=True)
    copy_code(output/'code',endpoint)
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


def main():
    parser=argparse.ArgumentParser();parser.add_argument('platform',choices=['macos','windows']);parser.add_argument('output',type=Path)
    parser.add_argument('--endpoint',type=Path,required=True);parser.add_argument('--runtime',type=Path);parser.add_argument('--runtime-sha256')
    parser.add_argument('--engine',type=Path)
    args=parser.parse_args();endpoint=json.loads(args.endpoint.read_text())
    if set(endpoint)!={'url','pin'}:raise ValueError('Endpoint metadata must contain only public URL and certificate pin')
    if args.platform=='macos':mac(args.output.resolve(),endpoint,args.engine)
    else:windows(args.output.resolve(),endpoint,args.runtime,args.runtime_sha256)
    print(args.output.resolve())


if __name__=='__main__':main()
