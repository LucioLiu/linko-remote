"""Synthetic click target for the pinned TLS upload A/B."""

import base64
from fractions import Fraction
import threading
import time

import av

from serial_avc_tls import FPS, GOP, HEIGHT, WIDTH, p95_ms
from benchmark import ScrollingText

MARKER_BITS = 8
CELL = 16


class InputDesktop:
    def __init__(self, viewer, agent, identifier, header, write):
        self.viewer, self.agent, self.identifier = viewer, agent, identifier
        self.header, self.write = header, write
        self.scene = ScrollingText(WIDTH, HEIGHT, 'full')
        self.encoder = av.CodecContext.create('libx264', 'w')
        self.encoder.width, self.encoder.height = WIDTH, HEIGHT
        self.encoder.time_base, self.encoder.framerate = Fraction(1, FPS), FPS
        self.encoder.pix_fmt, self.encoder.thread_count = 'yuv420p', 1
        self.encoder.gop_size, self.encoder.max_b_frames = GOP, 0
        self.encoder.options = {'profile': 'baseline', 'preset': 'veryfast', 'tune': 'zerolatency',
                                'crf': '26', 'x264-params': 'annexb=1:repeat-headers=1:scenecut=0:open-gop=0'}
        self.encoder.open()
        self.decoder = av.CodecContext.create('h264', 'r')
        self.decoder.thread_count = 1
        self.applied = 0
        self.first_frame = threading.Event()
        self.changed = threading.Condition()
        self.samples = {}

    @staticmethod
    def events(seq):
        # Click a distinct cell in the synthetic target for each input.
        return [{'type': kind, 'x': 512 * seq, 'y': 512, 'button': 0} for kind in ('down', 'up')]

    def clicks(self, end, errors):
        try:
            if not self.first_frame.wait(4):
                raise RuntimeError('No decoded frame before first input')
            for seq in range(1, 128):
                started = time.monotonic()
                if started + 1 >= end:
                    return
                with self.changed:
                    self.samples[seq] = {'input_seq': seq, 'submitted': started}
                self.viewer.call('live-input', {'id': self.identifier, 'seq': seq, 'geometry': '0,0,1280,720',
                                                'events': self.events(seq)})
                with self.changed:
                    self.samples[seq]['queued'] = time.monotonic()
                    self.changed.wait_for(lambda: {'ack', 'decoded'} <= self.samples[seq].keys(),
                                          timeout=min(1.75, max(0, end - time.monotonic())))
                time.sleep(.2)
        except Exception as exc:
            errors.append(str(exc))

    def capture(self, seq, captured):
        next_input = self.applied + 1
        name = f'{self.identifier}-{next_input:010d}.live-input.json'
        event = self.agent.read(name)
        if event.get('seq') == next_input:
            if event['id'] != self.identifier or event['events'] != self.events(next_input):
                raise ValueError('Unexpected input at synthetic click target')
            if event['expires'] <= time.time():
                raise ValueError('Input expired before reaching synthetic click target')
            self.applied = next_input
            with self.changed:
                self.samples[next_input]['applied'] = time.monotonic()
            self.write(self.agent.root / 'live-receipt.json', {'id': self.identifier, 'seq': next_input})

        rgb = self.scene.frame(seq - 1).copy()
        for bit in range(MARKER_BITS):
            offset = bit * CELL
            rgb[:CELL, offset:offset + CELL] = 255 if self.applied & (1 << bit) else 0
        frame = av.VideoFrame.from_ndarray(rgb, format='rgb24').reformat(format='yuv420p')
        frame.pts = seq - 1
        packet, = self.encoder.encode(frame)
        if packet.pts != seq - 1 or packet.is_keyframe != ((seq - 1) % GOP == 0):
            raise ValueError('Synthetic click encoder changed cadence')
        payload = bytes(packet)
        raw = self.header.pack(b'LPA1', len(payload), seq, int(captured * 1000), WIDTH, HEIGHT,
                               int(packet.is_keyframe), 0, 0, WIDTH, HEIGHT) + payload
        return base64.b64encode(raw).decode('ascii')

    def publish(self, frame, ack):
        with self.changed:
            ack_at = time.monotonic()
            for seq, sample in self.samples.items():
                if seq <= ack:
                    sample.setdefault('ack', ack_at)
            self.changed.notify_all()
        if not frame:
            return
        payload = base64.b64decode(frame['avc_base64'])[self.header.size:]
        decoded, = self.decoder.decode(av.Packet(payload))
        luma = decoded.planes[0]
        pixels = memoryview(luma)
        base = (CELL // 2) * luma.line_size + CELL // 2
        marker = sum((pixels[base + bit * CELL] >= 128) << bit for bit in range(MARKER_BITS))
        with self.changed:
            if marker:
                self.samples[marker].setdefault('decoded', time.monotonic())
                self.samples[marker].setdefault('frame_seq', frame['seq'])
            self.changed.notify_all()
        self.first_frame.set()

    def results(self):
        with self.changed:
            samples = [{
                'input_seq': seq, 'frame_seq': sample.get('frame_seq'),
                **{f'{key}_ms': round(1000 * (sample[key] - sample['submitted']), 3)
                   if key in sample else None for key in ('queued', 'applied', 'ack', 'decoded')},
            } for seq, sample in self.samples.items()]
        return {
            'inputs': samples,
            'ack_samples': sum(sample['ack_ms'] is not None for sample in samples),
            'decoded_samples': sum(sample['decoded_ms'] is not None for sample in samples),
            'input_ack_p95_ms': p95_ms([sample['ack_ms'] / 1000 for sample in samples if sample['ack_ms'] is not None]),
            'input_decoded_p95_ms': p95_ms([sample['decoded_ms'] / 1000 for sample in samples
                                           if sample['decoded_ms'] is not None]),
        }
