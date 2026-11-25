"""Compare full JPEG with frame-at-a-time H.264 on the existing scrolling fixture."""

import argparse
from fractions import Fraction
import json
import math
import platform
from statistics import median
from time import perf_counter_ns

import av
from av.video.frame import PictureType
import numpy as np

from benchmark import ScrollingText
from jpeg_delta import Jpeg


PIXEL_FORMAT = "yuvj444p"
JPEG_QUANTIZER = 5
SIZE_AND_FPS = ((1280, 720, 20), (1920, 1080, 30))
X264_PARAMS = (
    "bframes=0:rc-lookahead=0:sync-lookahead=0:scenecut=0:"
    "repeat-headers=1:annexb=1:open-gop=0"
)


class H264:
    def __init__(self, width, height, fps, crf):
        self.size = width, height
        self.time_base = Fraction(1, fps)
        self.encoder = av.CodecContext.create("libx264", "w")
        self.encoder.width, self.encoder.height = self.size
        self.encoder.time_base = self.time_base
        self.encoder.framerate = fps
        self.encoder.pix_fmt = PIXEL_FORMAT
        self.encoder.thread_count = 1
        self.gop_size = fps * 2
        self.encoder.gop_size = self.gop_size
        self.encoder.max_b_frames = 0
        self.encoder.options = {
            "preset": "veryfast",
            "tune": "zerolatency",
            "crf": str(crf),
            "x264-params": X264_PARAMS,
        }
        self.encoder.open()
        if self.encoder.options:
            raise RuntimeError(f"Unconsumed x264 options: {self.encoder.options}")
        self.decoder = av.CodecContext.create("h264", "r")
        self.decoder.thread_count = 1

    def encode(self, rgb, index):
        frame = av.VideoFrame.from_ndarray(rgb, format="rgb24")
        frame = frame.reformat(format=PIXEL_FORMAT)
        frame.pts = index
        frame.time_base = self.time_base
        packet, = self.encoder.encode(frame)
        if packet.pts != index or packet.dts != index:
            raise RuntimeError(f"Unexpected reorder or delay at frame {index}")
        return bytes(packet)

    def decode(self, payload, index):
        # Use a fresh decoder packet from the transmitted bytes. The sequence
        # number supplies the timestamp as it would in a framed transport.
        packet = av.Packet(payload)
        packet.pts = packet.dts = index
        packet.time_base = self.time_base
        frame, = self.decoder.decode(packet)
        expected_type = PictureType.P if index % self.gop_size else PictureType.I
        if (frame.pts != index or (frame.width, frame.height) != self.size
                or frame.pict_type != expected_type):
            raise RuntimeError(f"Invalid decoded frame for input {index}")
        return frame.to_ndarray(format="rgb24"), frame.pict_type == PictureType.I

    def finish(self):
        if self.encoder.encode(None) or self.decoder.decode(None):
            raise RuntimeError("Codec held output until flush")


class FullJpeg:
    def __init__(self, width, height):
        self.jpeg = Jpeg(JPEG_QUANTIZER)
        self.size = width, height

    def encode(self, rgb, index):
        return self.jpeg.encode(rgb)

    def decode(self, payload, index):
        return self.jpeg.decode(payload, *self.size), True

    def finish(self):
        encoder = self.jpeg.encoders[self.size]
        if encoder.encode(None) or self.jpeg.decoder.decode(None):
            raise RuntimeError("JPEG held output until flush")


class Quality:
    def __init__(self, text_edges):
        self.text_edges = text_edges
        self.rgb_squared_error = 0.0
        self.rgb_values = 0
        self.edge_squared_error = 0.0
        self.edge_values = 0
        self.gradient_squared_error = 0.0
        self.gradient_values = 0

    def add(self, original, decoded):
        rgb_error = decoded.astype(np.float32) - original
        self.rgb_squared_error += np.square(rgb_error).sum(dtype=np.float64)
        self.rgb_values += rgb_error.size
        if not self.text_edges:
            return

        # Horizontal gradients avoid counting the full-width row boundaries.
        # Green has the largest BT.601 luma weight and identifies glyph edges.
        gradient = np.diff(original[:, :, 1].astype(np.int16), axis=1)
        strong = np.abs(gradient) >= 24
        edge_pixels = np.zeros(original.shape[:2], dtype=bool)
        edge_pixels[:, :-1] |= strong
        edge_pixels[:, 1:] |= strong
        edge_error = rgb_error[edge_pixels]
        gradient_error = np.diff(rgb_error[:, :, 1], axis=1)[strong]
        self.edge_squared_error += np.square(edge_error).sum(dtype=np.float64)
        self.edge_values += edge_error.size
        self.gradient_squared_error += np.square(gradient_error).sum(dtype=np.float64)
        self.gradient_values += gradient_error.size

    def result(self):
        def psnr(squared_error, values):
            if squared_error == 0:
                return None  # Perfect reconstruction, PSNR is infinite.
            return round(10 * math.log10(255 ** 2 * values / squared_error), 3)

        result = {"rgb_psnr_db": psnr(self.rgb_squared_error, self.rgb_values)}
        if not self.text_edges:
            return result
        return {
            **result,
            "edge_rgb_psnr_db": psnr(self.edge_squared_error, self.edge_values),
            "edge_green_gradient_rmse": round(
                math.sqrt(self.gradient_squared_error / self.gradient_values), 3
            ),
        }


def measure_quality(codec, source, frames, *, text_edges=True):
    quality = Quality(text_edges)
    for index in range(frames):
        original = source.frame(index)
        decoded, _ = codec.decode(codec.encode(original, index), index)
        quality.add(original, decoded)
    codec.finish()
    return quality.result()


def calibrate(jpeg, h264, source, frames):
    # Score the full timing sequence, including its actual mix of I/P frames.
    target = measure_quality(jpeg(), source, frames)
    candidates = {}

    def score(crf):
        result = measure_quality(h264(crf), source, frames, text_edges=False)
        candidates[crf] = result
        return result["rgb_psnr_db"]

    target_psnr = target["rgb_psnr_db"]
    lower, upper = 20.0, 36.0
    low_psnr, high_psnr = score(lower), score(upper)
    if not low_psnr >= target_psnr >= high_psnr:
        raise RuntimeError("JPEG quality lies outside the H.264 CRF search range")
    for _ in range(6):
        middle = (lower + upper) / 2
        if score(middle) > target_psnr:
            lower = middle
        else:
            upper = middle
    best = min(candidates, key=lambda crf: abs(candidates[crf]["rgb_psnr_db"] - target_psnr))
    return best, target, candidates


class Timing:
    def __init__(self):
        self.sizes = []
        self.encode = []
        self.decode = []
        self.complete = []
        self.intra_frames = 0

    def add(self, codec, original, index):
        start = perf_counter_ns()
        payload = codec.encode(original, index)
        encoded = perf_counter_ns()
        _, intra = codec.decode(payload, index)
        done = perf_counter_ns()
        self.sizes.append(len(payload))
        self.encode.append(encoded - start)
        self.decode.append(done - encoded)
        self.complete.append(done - start)
        self.intra_frames += intra

    def result(self, fps):
        def latency(times):
            return {"p50": round(median(times) / 1e6, 3),
                    "p95": round(sorted(times)[math.ceil(len(times) * 0.95) - 1] / 1e6, 3)}

        total = sum(self.sizes)
        return {
            "payload_bytes": total,
            "mean_kib_per_frame": round(total / len(self.sizes) / 1024, 3),
            "estimated_mbps": round(total / len(self.sizes) * fps * 8 / 1e6, 3),
            "immediately_decoded_frames": len(self.sizes),
            "intra_frames": self.intra_frames,
            "encode_ms": latency(self.encode),
            "decode_ms": latency(self.decode),
            "encode_decode_ms": latency(self.complete),
        }


def compare(width, height, fps, args):
    source = ScrollingText(width, height, "full")

    def jpeg():
        return FullJpeg(width, height)

    def h264(crf):
        return H264(width, height, fps, crf)

    crf, jpeg_quality, trials = calibrate(jpeg, h264, source, args.frames)
    timings = {"full_jpeg": Timing(), "h264": Timing()}
    for repeat in range(args.repeats):
        codecs = [("full_jpeg", jpeg()), ("h264", h264(crf))]
        if repeat % 2:
            codecs.reverse()
        for index in range(args.frames):
            original = source.frame(index)
            for name, codec in codecs:
                timings[name].add(codec, original, index)
        for _, codec in codecs:
            codec.finish()

    results = {name: timing.result(fps) for name, timing in timings.items()}
    results["full_jpeg"]["quality"] = jpeg_quality
    results["h264"]["quality"] = measure_quality(h264(crf), source, args.frames)
    return {
        "width": width, "height": height, "fps": fps,
        "h264_crf": crf, "h264_gop_frames": 2 * fps, "h264_crf_trials": trials,
        "payload_savings_pct": round(
            100 * (1 - results["h264"]["payload_bytes"] / results["full_jpeg"]["payload_bytes"]), 2
        ),
        "h264_minus_jpeg_psnr_db": round(
            results["h264"]["quality"]["rgb_psnr_db"]
            - results["full_jpeg"]["quality"]["rgb_psnr_db"], 3
        ),
        **results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frames", type=int, default=120)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.frames < 2 or args.repeats < 1:
        parser.error("Require at least 2 frames and 1 repeat")
    report = {
        "platform": platform.platform(),
        "cpu": platform.processor(),
        "python": platform.python_version(),
        "pyav": av.__version__,
        "ffmpeg_libraries": av.library_versions,
        "parameters": vars(args),
        "jpeg_encoder": "mjpeg",
        "jpeg_quantizer": JPEG_QUANTIZER,
        "h264_encoder": "libx264",
        "h264_preset": "veryfast",
        "h264_tune": "zerolatency",
        "h264_params": X264_PARAMS,
        "encoder_threads": 1,
        "decoder_threads": 1,
        "pixel_format": PIXEL_FORMAT,
        "measurement": {
            "scene": "Existing full-screen fixture, scrolling 7 pixels per frame",
            "payload": "Raw JPEG or Annex B H.264, including in-band SPS/PPS on IDRs",
            "latency": "RGB to bytes to RGB, including color conversion and byte copies",
            "excluded": "RGB generation, capture, network, framing, queueing and rendering",
            "quality": "RGB PSNR from pooled pixel errors on every measured source frame",
            "text_edges": "RGB PSNR on pixels adjacent to horizontal green-channel jumps "
                          "of at least 24, plus green-gradient RMSE at those jumps",
            "calibration": "Full measured sequence at each resolution, minimizing the absolute RGB PSNR gap",
        },
        "resolutions": [compare(*spec, args) for spec in SIZE_AND_FPS],
    }
    print(json.dumps(report, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
