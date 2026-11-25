"""Replay the scored, synthetic 720p fixture from an SSH host through real WAN.

Run with Python from requirements.txt and an SSH configuration for that host.
The remote side needs Python 3. Only generated video and the sender are copied.
Both directions use one interactive SSH channel per run, including the start pulse.
"""
import argparse
import json
from pathlib import Path
import re
import shlex
import struct
import subprocess
import tarfile
import tempfile
import time

from h264_benchmark import FullJpeg, H264, ScrollingText


def read_frame(stream):
    header = stream.read(4)
    if len(header) != 4:
        raise EOFError("missing frame header")
    size, = struct.unpack("!I", header)
    if not 0 < size < 2_000_000:
        raise ValueError("unexpected fixture frame length")
    data = stream.read(size)
    if len(data) != size:
        raise EOFError("incomplete frame")
    return data


def replay(ssh, stage, name, codec, count):
    command = 'python3 ' + shlex.quote(stage + '/send.py') + ' ' + name
    with subprocess.Popen([*ssh, command], stdin=subprocess.PIPE, stdout=subprocess.PIPE) as proc:
        try:
            if proc.stdout.read(1) != b'R':
                raise RuntimeError("sender did not become ready")
            start = time.monotonic()
            proc.stdin.write(b'S')
            proc.stdin.flush()
            arrivals = []
            for index in range(count):
                codec.decode(read_frame(proc.stdout), index)
                arrivals.append(time.monotonic() - start)
        finally:
            proc.stdin.close()
            proc.stdout.close()
            if proc.wait(timeout=15):
                raise RuntimeError("remote sender failed")
    return dict(codec=name, frames=count, first_ms=round(1000*arrivals[0]),
                last_ms=round(1000*arrivals[-1]),
                steady_fps=round((count-1)/(arrivals[-1]-arrivals[0]),1),
                delivered_by_4s=sum(t<=4 for t in arrivals))


def benchmark(ssh):
    count = 80
    source = ScrollingText(1280,720,"full")
    codecs = {"h264":H264(1280,720,20,31.25), "jpeg":FullJpeg(1280,720)}
    with tempfile.TemporaryDirectory() as temp:
        folder = Path(temp)
        archive = folder / "synthetic.tgz"
        with tarfile.open(archive, "w:gz") as tar:
            for name, codec in codecs.items():
                file = folder / (name + ".bin")
                with file.open("wb") as out:
                    for index in range(count):
                        payload = codec.encode(source.frame(index), index)
                        out.write(struct.pack("!I",len(payload)))
                        out.write(payload)
                tar.add(file, arcname=file.name)
            tar.add(Path(__file__).with_name("wan_sender.py"), arcname="send.py")
        stage = subprocess.check_output([*ssh,"mktemp -d /tmp/linko-fixture.XXXXXXXX"],text=True).strip()
        if not re.fullmatch(r"/tmp/linko-fixture\.[A-Za-z0-9]{8}",stage):
            raise RuntimeError("unexpected staging directory")
        try:
            with archive.open("rb") as stream:
                subprocess.run([*ssh,"tar xzf - -C "+stage],stdin=stream,check=True)
            for name in ("h264","jpeg","jpeg","h264"):
                print(json.dumps(replay(ssh,stage,name,codecs[name],count)),flush=True)
        finally:
            files = [stage + "/" + f for f in ("send.py","h264.bin","jpeg.bin")]
            subprocess.run([*ssh,"rm -f "+" ".join(files)+" && rmdir "+stage],check=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host", help="SSH host alias")
    parser.add_argument("--ssh-config", type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]*",args.host):
        parser.error("expected an SSH host alias")
    ssh = ["ssh","-o","BatchMode=yes","-o","StrictHostKeyChecking=yes",
           "-o","ServerAliveInterval=10","-o","ServerAliveCountMax=3"]
    if args.ssh_config:
        ssh.extend(["-F",str(args.ssh_config)])
    benchmark([*ssh,args.host])
