"""Run a device's worker-only TLS gateway over an authenticated iroh endpoint."""
import argparse
import hashlib
import json
import os
import platform
import plistlib
import re
import secrets
import signal
import time
import subprocess
import tarfile
import tempfile
from pathlib import Path

from .core import Bridge, DEFAULT_HOME
from .worker_gateway import WorkerGateway

VERSION = "0.40.1"
RELEASE = f"https://github.com/n0-computer/dumbpipe/releases/download/v{VERSION}/"
DARWIN_SHA = {
    "arm64": ("aarch64", "e6bd3c9a98ba22e8233ae1f8ba5e2dceb4cbf132dc4d4332589295b702902a27"),
    "x86_64": ("x86_64", "eaa35e228528cbf5fc2a8a07646570f49af6ab1acd35c8f539f150a0cb5d4b0e")
}
WINDOWS_ZIP_SHA = "ffee0f811526e486992a12d3255ed26a84e932c76743009c50858ef15e4b046a"
WINDOWS_EXE_SHA = "820189a25dcfaa25629376569f75bc4f94dbec29df66902fdf512ffd572bb979"
PEER = DEFAULT_HOME / "peer"


def ensure_binary():
    arch, digest = DARWIN_SHA[platform.machine()]
    name = f"dumbpipe-v{VERSION}-darwin-{arch}.tar.gz"
    archive = PEER / name
    if not archive.exists():
        with tempfile.NamedTemporaryFile(dir=PEER) as download:
            subprocess.run(["curl", "-fLsS", "--max-time", "90", RELEASE + name,
                            "-o", download.name], check=True)
            if hashlib.sha256(Path(download.name).read_bytes()).hexdigest() != digest:
                raise ValueError("dumbpipe download hash mismatch")
            archive.write_bytes(Path(download.name).read_bytes())
    if hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
        raise ValueError("dumbpipe archive hash mismatch")
    with tarfile.open(archive) as tar:
        entry = next(e for e in tar if e.isfile() and Path(e.name).name == "dumbpipe")
        binary = tar.extractfile(entry).read()
    dest = PEER / "dumbpipe"
    if not dest.exists() or dest.read_bytes() != binary:
        with tempfile.NamedTemporaryFile(dir=PEER, delete=False) as tmp:
            tmp.write(binary)
            os.chmod(tmp.name, 0o700)
        os.replace(tmp.name, dest)
    return dest


def profile(device_id):
    path = PEER / (device_id + ".json")
    if not path.exists():
        data = json.dumps({"secret": secrets.token_hex(32), "alpn": secrets.token_hex(24)})
        with tempfile.NamedTemporaryFile(dir=PEER, delete=False) as temp:
            temp.write(data.encode())
        try:
            # Publish a complete identity; concurrent starts use the same winner.
            try:
                os.link(temp.name, path)
            except FileExistsError:
                pass
        finally:
            os.unlink(temp.name)
    return json.loads(path.read_text())


def serve(device):
    ident = device["id"]
    identity = profile(ident)
    binary = ensure_binary()
    with WorkerGateway(Path(device["runtime"]), 0) as gateway:
        proc = subprocess.Popen([str(binary), "listen-tcp", "-v", "--custom-alpn",
                                 identity["alpn"], "--host", f"127.0.0.1:{gateway.server_port}"],
                                env={**os.environ, "IROH_SECRET": identity["secret"]})
        def terminate(*_):
            proc.terminate()

        signal.signal(signal.SIGTERM, terminate)
        try:
            gateway.timeout = 1
            while proc.poll() is None:
                gateway.handle_request()
        finally:
            proc.terminate()
            proc.wait()


def enable(device):
    ident = device["id"]
    ensure_binary()
    profile(ident)
    label = "local.lanpilot.peer." + ident
    target = Path.home() / "Library/LaunchAgents" / (label + ".plist")
    domain = f"gui/{os.getuid()}"
    # A login agent cannot depend on macOS granting Desktop access to its shell.
    source = Path(__file__).resolve().parents[1]
    code = PEER / "code"
    for rel in ("desktop.py", "relay.py", "lanpilot/__init__.py", "lanpilot/transport.py",
                "lanpilot/core.py", "lanpilot/worker_gateway.py", "lanpilot/peer.py"):
        dest = code / rel
        dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        data = (source / rel).read_bytes()
        if not dest.exists() or dest.read_bytes() != data:
            with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as tmp:
                tmp.write(data)
            os.replace(tmp.name, dest)
    log = str(PEER / (ident + ".log"))
    content = plistlib.dumps(dict(Label=label, ProgramArguments=[
        "/usr/bin/python3", "-m", "lanpilot.peer", "serve", ident],
        WorkingDirectory=str(code), KeepAlive=True, RunAtLoad=True,
        StandardErrorPath=log, StandardOutPath=log, ThrottleInterval=10))
    if not target.exists() or target.read_bytes() != content:
        subprocess.run(["launchctl", "bootout", domain + "/" + label], capture_output=True)
        for _ in range(40):
            if subprocess.run(["launchctl", "print", domain + "/" + label], capture_output=True).returncode:
                break
            time.sleep(0.1)
        target.write_bytes(content)
        target.chmod(0o600)
    active = subprocess.run(["launchctl", "print", domain + "/" + label], capture_output=True)
    if active.returncode:
        subprocess.run(["launchctl", "bootstrap", domain, str(target)], check=True)


def disable(device):
    label = "local.lanpilot.peer." + device["id"]
    domain = f"gui/{os.getuid()}/{label}"
    if subprocess.run(["launchctl", "print", domain], capture_output=True).returncode == 0:
        subprocess.run(["launchctl", "bootout", domain], check=True)
    (Path.home() / "Library/LaunchAgents" / (label + ".plist")).unlink(missing_ok=True)


def export_windows(device, output):
    ident = device["id"]
    log = (PEER / (ident + ".log")).read_text()
    full = re.findall(r"To connect, use e.g.:\s*dumbpipe connect-tcp (endpoint[a-z2-7]+)", log)
    if not full:
        raise ValueError("No endpoint ticket yet. Retry after the listener starts.")
    alpn = profile(ident)["alpn"]
    template = (Path(__file__).resolve().parents[1] / "windows/Install-PeerRoute.ps1").read_text()
    pin = json.loads((Path(device["runtime"]) / "config.json").read_text())["pin"]
    content = (template.replace("@TICKET@", full[-1]).replace("@ALPN@", alpn).replace("@PIN@", pin)
               .replace("@VERSION@", VERSION).replace("@ZIP_SHA@", WINDOWS_ZIP_SHA)
               .replace("@EXE_SHA@", WINDOWS_EXE_SHA))
    with output.open("x", encoding="utf-8-sig") as file:
        os.chmod(output, 0o600)
        file.write(content)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("serve", "enable", "disable", "export"):
        sub = commands.add_parser(command)
        sub.add_argument("device")
        if command == "export":
            sub.add_argument("output", type=Path)
    args = parser.parse_args()
    PEER.mkdir(mode=0o700, parents=True, exist_ok=True)
    device = Bridge().device(args.device)
    if args.command == "serve":
        serve(device)
    elif args.command == "enable":
        enable(device)
    elif args.command == "disable":
        disable(device)
    else:
        export_windows(device, args.output)


if __name__ == "__main__":
    main()
