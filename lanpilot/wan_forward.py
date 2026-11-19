"""Manage a persistent device-specific raw TCP forward using bore on macOS."""
import argparse
import fcntl
import json
import json
import os
import plistlib
import re
import socket
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from .core import Bridge

RESOURCES = Path(__file__).resolve().parents[1]
AGENTS = Path.home() / "Library/LaunchAgents"
WAN = Path.home() / "Library/Application Support/LanPilot/WAN"


def port_arg(value):
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def run_launchctl(*args, check=True):
    return subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True,
                          check=check)


def label(role, device):
    return f"local.lanpilot.wan.{role}.{device}"


def definition(name, command):
    return {
        "Label": name, "ProgramArguments": command, "WorkingDirectory": str(RESOURCES),
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 15,
        "StandardOutPath": str(WAN / (name + ".log")),
        "StandardErrorPath": str(WAN / (name + ".err")),
    }


def load_definition(name):
    path = AGENTS / (name + ".plist")
    return plistlib.loads(path.read_bytes()) if path.exists() else None


def atomic_write(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as temp:
        tmp = Path(temp.name)
        try:
            temp.write(data)
            temp.flush()
            os.fsync(temp.fileno())
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)


def launch_definition(config):
    name = config["Label"]
    target = f"gui/{os.getuid()}/{name}"
    path = AGENTS / (name + ".plist")
    previous = load_definition(name)
    loaded = run_launchctl("print", target, check=False).returncode == 0
    if previous == config and loaded:
        return
    if loaded:
        run_launchctl("bootout", target)
        # Supervisors drain their child process on SIGTERM. launchctl bootout
        # may return while the old job still occupies its registration.
        for _ in range(100):
            if run_launchctl("print",target,check=False).returncode != 0:break
            time.sleep(.1)
    try:
        atomic_write(path, plistlib.dumps(config))
        run_launchctl("bootstrap", f"gui/{os.getuid()}", str(path))
    except Exception:
        if previous:
            atomic_write(path, plistlib.dumps(previous))
            if loaded:
                run_launchctl("bootstrap", f"gui/{os.getuid()}", str(path))
        else:
            path.unlink(missing_ok=True)
        raise


@contextmanager
def deployment_lock():
    WAN.mkdir(mode=0o700, parents=True, exist_ok=True)
    AGENTS.mkdir(parents=True, exist_ok=True)
    with (WAN / "forward.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def private_secret(path):
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("bore secret must be a private owner-only file")
    secret = path.read_text().rstrip("\r\n")
    if not secret or len(secret) > 4096 or "\n" in secret:
        raise ValueError("invalid bore secret file")
    return secret


def install(device, server, remote, local, binary, secret_file=None):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", server):
        raise ValueError("server must be the DNS name or IPv4 address of a bore server")
    binary = binary.resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError("provide an installed bore binary with --bore")
    gateway = label("gateway", device)
    tunnel = label("tunnel", device)
    with deployment_lock():
        previous = load_definition(gateway)
        if local is None:
            if previous:
                args = previous["ProgramArguments"]
                local = int(args[args.index("--port") + 1])
            else:
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    local = sock.getsockname()[1]
        for path in AGENTS.glob("local.lanpilot.wan.tunnel.*.plist"):
            other = plistlib.loads(path.read_bytes())
            if other["Label"] == tunnel:
                continue
            arguments = other["ProgramArguments"]
            if (arguments[arguments.index("--to") + 1].lower() == server.lower()
                    and arguments[arguments.index("--port") + 1] == str(remote)):
                raise ValueError("public server port already assigned to another device")
        old_port = (int(previous["ProgramArguments"][-1]) if previous else None)
        if local != old_port:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", local))
        # Retain old definitions so a failed tunnel change can restore the
        # gateway it originally pointed to, including on a first-time setup.
        forward_args = [str(binary), "local", str(local), "--local-host",
                        "127.0.0.1", "--to", server, "--port", str(remote)]
        if secret_file:
            private_secret(secret_file)
            forward_args = ["/usr/bin/python3", "-m", "lanpilot.wan_forward", "_forward",
                            str(secret_file.resolve()), *forward_args]
        cfg=json.loads((Path(Bridge().device(device)['runtime'])/'config.json').read_text())
        forward_args=['/usr/bin/python3','-m','lanpilot.tunnel_supervisor',
                      '--host',server,'--remote',str(remote),'--pin',cfg['pin'],'--',*forward_args]
        configs = [
            definition(gateway, ["/usr/bin/python3", "-m", "lanpilot.worker_gateway",
                                 "--device", device, "--port", str(local)]),
            definition(tunnel, forward_args),
        ]
        old_gateway = load_definition(gateway)
        launch_definition(configs[0])
        try:
            launch_definition(configs[1])
        except Exception:
            if old_gateway:
                launch_definition(old_gateway)
            else:
                disable(gateway)
            raise
    return {"device": device, "url": f"https://{server}:{remote}",
            "gateway_port": local, "state": "launch_agents_installed"}


def disable(name):
    target = f"gui/{os.getuid()}/{name}"
    if run_launchctl("print", target, check=False).returncode == 0:
        run_launchctl("bootout", target)
    (AGENTS / (name + ".plist")).unlink(missing_ok=True)


def main():
    if len(sys.argv) > 3 and sys.argv[1] == "_forward":
        os.environ["BORE_SECRET"] = private_secret(Path(sys.argv[2]))
        os.execv(sys.argv[3], sys.argv[3:])
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    add = commands.add_parser("enable")
    add.add_argument("device", help="registered device ID or unique name")
    add.add_argument("--server", required=True, help="public bore server")
    add.add_argument("--port", required=True, type=port_arg, help="stable public port")
    add.add_argument("--local-port", type=port_arg)
    add.add_argument("--bore", type=Path, default=WAN / "bin/bore")
    add.add_argument("--secret-file", type=Path, help="owner-only file for your private bore server")
    remove = commands.add_parser("disable")
    remove.add_argument("device")
    args = parser.parse_args()
    device = Bridge().device(args.device)["id"]
    if args.command == "disable":
        with deployment_lock():
            disable(label("tunnel", device))
            disable(label("gateway", device))
        print(json.dumps({"device": device, "state": "disabled"}))
        return
    print(json.dumps(install(device, args.server, args.port, args.local_port, args.bore, args.secret_file)))


if __name__ == "__main__":
    main()
