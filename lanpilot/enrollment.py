"""Create a device-specific installer using only the Windows user's public key."""
import contextlib
import fcntl
import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
import zipfile

from relay import init_runtime, pair

WINDOWS = Path(__file__).resolve().parents[1] / "windows"
INSTALL_FILES = ("Desktop.cs", "Discovery.cs", "Relay.cs", "Start-Desktop.ps1",
                 "LanPilot.ps1", "Launcher.cs", "UserStartup.ps1", "WorkerRunspace.ps1", "LocalJob.ps1", "LocalJobHost.ps1", "LedgerReconcile.cs", "Install-LanPilot.ps1")


def local_address():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect(("192.0.2.1", 9))
            return sock.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def enroll(bridge, name, key, output, host=None):
    with (bridge.home / "enrollment.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _enroll(bridge, name, key, output, host)


def available_port(used):
    for port in range(18771, 65536):
        if port in used:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            try:
                probe.bind(("0.0.0.0", port))
            except OSError:
                continue
            return port
    raise ValueError("没有可分配的设备端口")


def _enroll(bridge, name, key, output, host):
    name = name.strip()
    if not name or len(name) > 120:
        raise ValueError("请输入有效的设备名称")
    with key.open("rb") as stream:
        if len(stream.read(16385)) > 16384:
            raise ValueError("配对公钥文件太大")
    address = str(ipaddress.IPv4Address(host or local_address()))
    devices_dir = bridge.home / "devices"
    devices_dir.mkdir(mode=0o700, exist_ok=True)
    runtime = Path(tempfile.mkdtemp(prefix="device-", dir=devices_dir))
    tmp = None
    published = False
    try:
        devices = bridge.devices()
        if any(device["name"] == name for device in devices):
            raise ValueError("设备名称已使用")
        used = {json.loads((Path(device["runtime"]) / "config.json").read_text())["port"] for device in devices}
        port = available_port(used)
        with contextlib.redirect_stdout(io.StringIO()):
            init_runtime(runtime, address, port)
            pair(runtime, key, runtime / "pairing.bin")
        public_key = (runtime / "device-public.pem").read_bytes()
        for device in devices:
            prior = Path(device["runtime"]) / "device-public.pem"
            if prior.exists() and prior.read_bytes() == public_key:
                raise ValueError("此 Windows 账户已配对，请使用原设备和配对包")
        with tempfile.NamedTemporaryFile(dir=output.parent, delete=False) as archive:
            tmp = Path(archive.name)
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as archive:
            checksums = {}
            for filename in INSTALL_FILES:
                data = (WINDOWS / filename).read_bytes()
                archive.writestr(filename, data)
                checksums[filename] = hashlib.sha256(data).hexdigest().upper()
            archive.write(runtime / "pairing.bin", "pairing.bin")
            archive.writestr("manifest.json", json.dumps(checksums))
            archive.writestr("Install.cmd", '@echo off\r\npowershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install-LanPilot.ps1"\r\n')
        # Same-filesystem link publishes the complete kit without overwriting any file.
        os.link(tmp, output)
        published = True
        device = bridge.adopt(name, runtime)
    except BaseException:
        if published:
            output.unlink()
        shutil.rmtree(runtime)
        raise
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
    return {"id": device["id"], "name": name, "installer": str(output), "port": port}
