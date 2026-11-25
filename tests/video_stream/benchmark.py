"""Compare standalone JPEGs and tile updates on the same generated RGB frames."""

import argparse
import json
import math
import platform
from statistics import median
from time import perf_counter_ns

import av
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from jpeg_delta import Jpeg, Receiver, Sender, TILE, full_packet


class ScrollingText:
    def __init__(self, width, height, scene):
        self.width = width
        self.height = height
        self.top = height // 5 if scene == "region" else 0
        self.bottom = height * 5 // 8 if scene == "region" else height
        self.panel_height = self.bottom - self.top
        self.step = 26
        self.period = self.step * 72
        self.font = self.font_for_text()
        self.bar_font = ImageFont.load_default(size=16)
        self.page = Image.new("RGB", (width, height), "#edf1f6")
        page = ImageDraw.Draw(self.page)
        page.rectangle((0, 0, width, 48), fill="#20344c")
        page.text((16, 16), "Work queue  /  weekly operations", font=self.bar_font,
                  fill="#ffffff")
        for y in range(self.bottom + 12, height, self.step):
            page.text((16, y), "Pinned: attendance  |  changes  |  notes",
                      font=self.font, fill="#263850")

        self.strip = Image.new("RGB", (width, self.period + self.panel_height))
        strip = ImageDraw.Draw(self.strip)
        for y in range(0, self.strip.height, self.step):
            row = (y // self.step) % 72
            strip.rectangle((0, y, width, y + self.step - 1),
                            fill=("#ffffff" if row % 2 else "#dce9f4"))
            for column, x in enumerate(range(12, width, 212)):
                strip.text((x, y + 4), f"{row:03d}   Ref {row * 17 + column:04d}   Review",
                           font=self.font, fill="#253c59")

    @staticmethod
    def font_for_text():
        return ImageFont.load_default(size=14)

    def frame(self, index):
        frame = self.page.copy()
        scroll = index * 7 % self.period
        panel = self.strip.crop((0, scroll, self.width, scroll + self.panel_height))
        frame.paste(panel, (0, self.top))
        draw = ImageDraw.Draw(frame)
        color = ("#9a3434", "#26589a", "#23725c")[index // 9 % 3]
        draw.rectangle((8, self.top + 4, 171, self.top + 25), fill=color)
        draw.text((14, self.top + 6), f"SYNC {index:04d}", font=self.font, fill="#ffffff")
        return np.asarray(frame)


class Measurements:
    def __init__(self):
        self.bytes = []
        self.encode = []
        self.decode = []
        self.complete = []
        self.full = 0

    def run(self, send, receiver, rgb, sequence):
        started = perf_counter_ns()
        packet = send(rgb, sequence)
        sent = perf_counter_ns()
        frame = receiver.receive(packet)
        received = perf_counter_ns()
        self.bytes.append(len(packet))
        self.encode.append(sent - started)
        self.decode.append(received - sent)
        self.complete.append(received - started)
        self.full += packet[:1] == b"F"
        return frame

    def result(self, fps):
        def milliseconds(values):
            p95 = sorted(values)[math.ceil(len(values) * 0.95) - 1]
            return {"p50": round(median(values) / 1e6, 3), "p95": round(p95 / 1e6, 3)}

        total = sum(self.bytes)
        return {
            "packet_bytes": total,
            "mean_kib_per_frame": round(total / len(self.bytes) / 1024, 3),
            "estimated_mbps_at_fixture_fps": round(total / len(self.bytes) * fps * 8 / 1e6, 3),
            "full_frames": self.full,
            "delta_frames": len(self.bytes) - self.full,
            "encode_ms": milliseconds(self.encode),
            "decode_ms": milliseconds(self.decode),
            "encode_decode_ms": milliseconds(self.complete),
        }


def compare(args, scene):
    source = ScrollingText(args.width, args.height, scene)
    results = {name: Measurements() for name in ("full_jpeg", "jpeg_delta")}
    total_frames = 0

    for repeat in range(args.repeats):
        full = Jpeg(args.quantizer)
        delta = Sender(Jpeg(args.quantizer), key_interval=args.fps * 2)
        methods = {
            "full_jpeg": (lambda rgb, seq: full_packet(full, rgb, seq),
                          Receiver(Jpeg(args.quantizer))),
            "jpeg_delta": (lambda rgb, seq: delta.send(rgb),
                           Receiver(Jpeg(args.quantizer))),
        }
        order = list(methods)
        if repeat % 2:
            order.reverse()
        for index in range(args.frames):
            rgb = source.frame(index)
            frames = {}
            for name in order:
                send, receiver = methods[name]
                frames[name] = results[name].run(send, receiver, rgb, index + 1)
            if not np.array_equal(frames["full_jpeg"], frames["jpeg_delta"]):
                raise RuntimeError(f"{scene}, repeat {repeat + 1}, frame {index + 1}: "
                                   "reconstructed RGB differs from full JPEG")
            total_frames += 1

    summary = {name: result.result(args.fps) for name, result in results.items()}
    ratio = summary["jpeg_delta"]["packet_bytes"] / summary["full_jpeg"]["packet_bytes"]
    return {
        "equal_rgb_frames": total_frames,
        "wire_byte_savings_pct": round((1 - ratio) * 100, 2),
        **summary,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=20, help="Target rate for bitrate math")
    parser.add_argument("--frames", type=int, default=120, help="Frames per scene per repeat")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--quantizer", type=int, default=5)
    parser.add_argument("--scene", choices=("both", "region", "full"), default="both")
    args = parser.parse_args()
    if not (256 <= args.width <= 7680 and 256 <= args.height <= 4320
            and 1 <= args.fps <= 120 and args.frames >= 2 and args.repeats >= 1
            and 2 <= args.quantizer <= 31):
        parser.error("Choose a 256–7680 × 256–4320 frame, 1–120 fps, ≥2 frames, "
                     "≥1 repeat and a quantizer of 2–31")
    scenes = ("region", "full") if args.scene == "both" else (args.scene,)
    print(json.dumps({
        "platform": platform.platform(),
        "python": platform.python_version(),
        "pyav": av.__version__,
        "libavcodec": ".".join(map(str, av.library_versions["libavcodec"])),
        "tile_px": TILE,
        "parameters": vars(args),
        "scenes": {scene: compare(args, scene) for scene in scenes},
    }, indent=2))


if __name__ == "__main__":
    main()
