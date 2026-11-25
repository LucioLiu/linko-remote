"""Experimental ordered, binary JPEG tile stream for local benchmarks."""

from struct import Struct

import av
import numpy as np

TILE = 128
FRAME = Struct("!cIIHH")  # kind, sequence, base sequence, width, height
STRIP = Struct("!HHHI")  # x, y, width, JPEG byte length


class MissingBase(ValueError):
    """The next update must be a complete JPEG."""


class Jpeg:
    """Use the same single-thread FFmpeg MJPEG settings for frames and tiles."""

    def __init__(self, quantizer=5):
        self.quantizer = quantizer
        self.encoders = {}
        self.decoder = av.CodecContext.create("mjpeg", "r")
        self.decoder.thread_count = 1

    def encode(self, rgb):
        height, width = rgb.shape[:2]
        size = width, height
        if size not in self.encoders:
            codec = av.CodecContext.create("mjpeg", "w")
            codec.width, codec.height = size
            codec.pix_fmt = "yuvj444p"
            codec.thread_count = 1
            codec.options = {"qmin": str(self.quantizer), "qmax": str(self.quantizer)}
            self.encoders[size] = codec
        codec = self.encoders[size]
        frame = av.VideoFrame.from_ndarray(rgb, format="rgb24")
        packet, = codec.encode(frame.reformat(format=codec.pix_fmt))
        return bytes(packet)

    def decode(self, payload, width, height):
        frame, = self.decoder.decode(av.Packet(payload))
        if (frame.width, frame.height) != (width, height):
            raise ValueError("JPEG dimensions do not match the packet")
        return frame.to_ndarray(format="rgb24")


def full_packet(jpeg, rgb, sequence):
    height, width = rgb.shape[:2]
    return FRAME.pack(b"F", sequence, 0, width, height) + jpeg.encode(rgb)


class Sender:
    def __init__(self, jpeg, key_interval=40, max_dirty_fraction=0.60):
        self.jpeg = jpeg
        self.key_interval = key_interval
        self.max_dirty_fraction = max_dirty_fraction
        self.previous = None
        self.sequence = 0
        self.last_full = 0

    def _delta(self, rgb, sequence):
        height, width = rgb.shape[:2]
        tile_count = ((width + TILE - 1) // TILE) * ((height + TILE - 1) // TILE)
        strips = []
        dirty = 0
        for y in range(0, height, TILE):
            row = rgb[y:y + TILE]
            previous = self.previous[y:y + TILE]
            if np.array_equal(row, previous):
                continue

            start = None
            for x in range(0, width, TILE):
                if not np.array_equal(row[:, x:x + TILE], previous[:, x:x + TILE]):
                    dirty += 1
                    if dirty > tile_count * self.max_dirty_fraction:
                        return None
                    if start is None:
                        start = x
                elif start is not None:
                    strips.append((start, y, x - start))
                    start = None
            if start is not None:
                strips.append((start, y, width - start))

        parts = [FRAME.pack(b"D", sequence, self.sequence, width, height)]
        for x, y, strip_width in strips:
            payload = self.jpeg.encode(rgb[y:y + TILE, x:x + strip_width])
            parts.extend((STRIP.pack(x, y, strip_width, len(payload)), payload))
        return b"".join(parts)

    def send(self, rgb, force_full=False):
        sequence = self.sequence + 1
        packet = None
        if (
            not force_full
            and self.previous is not None
            and rgb.shape == self.previous.shape
            and sequence - self.last_full < self.key_interval
        ):
            packet = self._delta(rgb, sequence)
        if packet is None:
            packet = full_packet(self.jpeg, rgb, sequence)
            self.last_full = sequence
        self.previous = rgb.copy()
        self.sequence = sequence
        return packet


class Receiver:
    def __init__(self, jpeg):
        self.jpeg = jpeg
        self.sequence = 0
        self.frame = None

    def _apply_delta(self, payload, width, height):
        if not payload:
            return self.frame

        updates = []
        previous_y = -1
        previous_end = 0
        offset = 0
        while offset < len(payload):
            if len(payload) - offset < STRIP.size:
                raise ValueError("Incomplete strip header")
            x, y, strip_width, size = STRIP.unpack_from(payload, offset)
            offset += STRIP.size
            end = x + strip_width
            if (
                y >= height or y % TILE or x % TILE or not strip_width or end > width
                or (end < width and end % TILE)
                or y < previous_y or (y == previous_y and x < previous_end)
            ):
                raise ValueError("Invalid, overlapping or out-of-order strip")
            if not 0 < size <= len(payload) - offset:
                raise ValueError("Invalid strip payload size")

            strip = self.jpeg.decode(payload[offset:offset + size],
                                     strip_width, min(TILE, height - y))
            updates.append((x, y, strip))
            previous_y, previous_end = y, end
            offset += size

        result = self.frame.copy()
        for x, y, strip in updates:
            strip_height, strip_width = strip.shape[:2]
            result[y:y + strip_height, x:x + strip_width] = strip
        return result

    def receive(self, packet):
        if len(packet) < FRAME.size:
            raise ValueError("Incomplete frame header")
        kind, sequence, base, width, height = FRAME.unpack_from(packet)
        if sequence <= self.sequence or not width or not height:
            raise ValueError("Stale sequence or invalid dimensions")

        payload = memoryview(packet)[FRAME.size:]
        if kind == b"F" and base == 0 and payload:
            frame = self.jpeg.decode(payload, width, height)
        elif kind == b"D":
            if self.frame is None or base != self.sequence or sequence != base + 1:
                raise MissingBase(f"Sequence {sequence} requires frame {base}")
            if self.frame.shape[:2] != (height, width):
                raise ValueError("Delta dimensions changed")
            frame = self._apply_delta(payload, width, height)
        else:
            raise ValueError("Invalid frame type or payload")

        frame.setflags(write=False)
        self.frame = frame
        self.sequence = sequence
        return frame
