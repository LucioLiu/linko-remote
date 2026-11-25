"""Build and exercise the standalone VideoToolbox decoder on macOS."""

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import struct
import subprocess
import tempfile
import time

import av
from PIL import Image, ImageDraw


FPS = 30
GOP = 30
MAX_AU_BYTES = 2 * 1024 * 1024
START_CODE = re.compile(rb"\x00\x00(?:\x00)*\x01")


def nals(packet):
    return START_CODE.split(packet)[1:]


def encode_scroll(width, height, count, profile="main", pixel_format="yuv420p"):
    canvas = Image.new("RGB", (width, height * 3), "#192335")
    draw = ImageDraw.Draw(canvas)
    for row in range(height * 3 // 24):
        y = row * 24
        draw.rectangle((8, y, width - 8, y + 21), fill=("#314764", "#272e3a")[row % 2])
        draw.text((18, y + 4), f"{row:04d}  Scrolling row  /  VideoToolbox sample " * 4,
                  fill=("#f0f4fa", "#a9e0be")[row % 2])

    encoder = av.CodecContext.create("libx264", "w")
    encoder.width = width
    encoder.height = height
    encoder.pix_fmt = pixel_format
    encoder.framerate = FPS
    encoder.options = {
        "profile": profile, "preset": "ultrafast", "tune": "zerolatency",
        "x264-params": f"annexb=1:repeat-headers=1:keyint={GOP}:scenecut=0:bframes=0:slices=2",
    }
    packets = []
    for index in range(count):
        top = index * 7 % (height * 2)
        image = canvas.crop((0, top, width, top + height))
        frame = av.VideoFrame.from_image(image).reformat(format=pixel_format)
        packets.extend(encoder.encode(frame))
    packets.extend(encoder.encode(None))
    assert len(packets) == count
    for index, packet in enumerate(packets):
        types = [nal[0] & 31 for nal in nals(bytes(packet))]
        if index % GOP == 0:
            assert 7 in types and 8 in types and types.count(5) >= 2 and 1 not in types, types
        else:
            assert types.count(1) >= 2 and 5 not in types, types
    return packets


def plane_digest(frame):
    # Software decode produces planar YUV. NV12 changes only the chroma layout.
    assert frame.format.name == "yuv420p", frame.format.name
    luma, u, v = frame.planes
    ydata, udata, vdata = map(memoryview, frame.planes)
    digest = hashlib.sha256()
    for row in range(luma.height):
        y_start = row * luma.line_size
        digest.update(ydata[y_start:y_start + luma.width])
    uv = bytearray(u.width * 2)
    for row in range(u.height):
        u_start = row * u.line_size
        v_start = row * v.line_size
        uv[::2] = udata[u_start:u_start + u.width]
        uv[1::2] = vdata[v_start:v_start + v.width]
        digest.update(uv)
    return digest.hexdigest()


def reference(packets):
    decoder = av.CodecContext.create("h264", "r")
    digests = []
    for packet in packets:
        digests.extend(plane_digest(frame) for frame in decoder.decode(packet))
    digests.extend(plane_digest(frame) for frame in decoder.decode(None))
    assert len(digests) == len(packets) and len(set(digests)) > 1
    return digests


def run_cli(cli, records, paced=False):
    # Spool output to a file so the pipe feeding the decoder can never deadlock on stdout.
    with tempfile.TemporaryFile() as output:
        with subprocess.Popen([cli], stdin=subprocess.PIPE, stdout=output) as process:
            started = time.monotonic()
            for index, payload in enumerate(records):
                if paced:
                    time.sleep(max(0, started + index / FPS - time.monotonic()))
                process.stdin.write(struct.pack(">I", len(payload)) + payload)
                process.stdin.flush()
            process.stdin.close()
            assert process.wait(timeout=15) == 0
        output.seek(0)
        rows = [json.loads(line) for line in output]
    assert [row["index"] for row in rows] == list(range(1, len(records) + 1))
    return rows


def check_frame(row, size, digest, require_hardware):
    assert row["kind"] == "frame", row
    assert (row["width"], row["height"]) == size, (size, row)
    assert row["sha256"] == digest, (digest, row)
    if require_hardware:
        assert row["hardware"] is True, row


def percentile(values, quantile):
    values = sorted(values)
    return values[math.ceil(len(values) * quantile) - 1]


def verify_scroll(cli, packets, digests, size, require_hardware):
    rows = run_cli(cli, list(map(bytes, packets)), paced=True)
    for row, digest in zip(rows, digests, strict=True):
        check_frame(row, size, digest, require_hardware)
    timing = [row["decode_ms"] for row in rows]
    return {
        "size": list(size), "fps": FPS, "frames": len(rows), "pixel_matches": len(rows),
        "hardware_frames": sum(row["hardware"] is True for row in rows),
        "software_frames": sum(row["hardware"] is False for row in rows),
        "hardware_unknown": sum(row["hardware"] is None for row in rows),
        "first_ms": round(timing[0], 3),
        "steady_p50_ms": round(percentile(timing[10:], 0.50), 3),
        "steady_p95_ms": round(percentile(timing[10:], 0.95), 3),
    }


def split_initial_packet(packet):
    units = nals(bytes(packet))
    # Recompose with three-byte prefixes to exercise both Annex B delimiter lengths.
    parameters = b"".join(b"\x00\x00\x01" + nal for nal in units if nal[0] & 31 in (7, 8))
    picture = b"".join(b"\x00\x00\x01" + nal for nal in units if nal[0] & 31 not in (7, 8))
    return parameters, picture


def verify_recovery(cli, low, high, require_hardware):
    low_packets, low_hashes, low_size = low
    high_packets, high_hashes, high_size = high
    low_params, low_idr = split_initial_packet(low_packets[0])
    _, low_next_idr = split_initial_packet(low_packets[GOP])
    high_params, high_idr = split_initial_packet(high_packets[0])
    unsupported = bytes(encode_scroll(1280, 720, 1, "high444", "yuv444p")[0])
    payloads = [
        low_idr, low_params, bytes(low_packets[1]), low_idr, bytes(low_packets[1]),
        b"", bytes(low_packets[2]), low_next_idr, bytes(low_packets[GOP + 1]),
        high_params, bytes(high_packets[1]), high_idr, bytes(high_packets[1]),
        b"\x00\x00\x01", bytes(high_packets[2]), high_idr, unsupported, high_idr,
    ]
    rows = run_cli(cli, payloads)
    assert [row["kind"] for row in rows] == [
        "needsIDR", "noPicture", "needsIDR", "frame", "frame",
        "reset", "needsIDR", "frame", "frame",
        "noPicture", "needsIDR", "frame", "frame",
        "decodeError", "needsIDR", "frame", "decodeError", "frame",
    ], rows
    assert rows[16]["error"] == "unsupportedFormat", rows[16]
    low_frames = {3: 0, 4: 1, 7: GOP, 8: GOP + 1}
    high_frames = {11: 0, 12: 1, 15: 0, 17: 0}
    for row, index in low_frames.items():
        check_frame(rows[row], low_size, low_hashes[index], require_hardware)
    for row, index in high_frames.items():
        check_frame(rows[row], high_size, high_hashes[index], require_hardware)
    for bad_input, error in [
        (b"\x00\x01", b"truncatedRecord"),
        (struct.pack(">I", 8) + b"\x00\x00\x01", b"truncatedRecord"),
        (struct.pack(">I", MAX_AU_BYTES + 1), b"oversizedRecord"),
    ]:
        process = subprocess.run([cli], input=bad_input, capture_output=True, timeout=10)
        assert process.returncode == 1 and error in process.stderr, process


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, default=240, help="frames per resolution, at least 32")
    parser.add_argument("--allow-software", action="store_true", help="also accept a software decoder")
    args = parser.parse_args()
    if args.frames < GOP + 2:
        parser.error("--frames must be at least 32")

    sources = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="lanpilot-vt-") as work:
        cli = Path(work) / "decode-cli"
        subprocess.run(["sh", sources / "build.sh", cli], check=True, timeout=60)
        fixtures = []
        for size in [(1280, 720), (1920, 1080)]:
            packets = encode_scroll(*size, args.frames)
            digests = reference(packets)
            fixtures.append((packets, digests, size))
            print(json.dumps(verify_scroll(cli, packets, digests, size, not args.allow_software)),
                  flush=True)
        verify_recovery(cli, *fixtures, not args.allow_software)
        print("PASS: parameter sets, loss, new resolution, malformed input, 4:4:4 rejection and record limits")


if __name__ == "__main__":
    main()
