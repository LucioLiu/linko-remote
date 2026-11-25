"""Send length-prefixed, pre-encoded test frames at the fixture's target cadence."""
from pathlib import Path
import struct
import sys
import time


def send(name):
    if name not in ('h264', 'jpeg'):
        raise ValueError('unknown fixture')
    stream = sys.stdout.buffer
    with Path(__file__).with_name(name + '.bin').open('rb') as source:
        stream.write(b'R')
        stream.flush()
        if sys.stdin.buffer.read(1) != b'S':
            raise RuntimeError('missing client start pulse')
        start = time.monotonic()
        index = 0
        while prefix := source.read(4):
            size, = struct.unpack('!I', prefix)
            payload = source.read(size)
            if len(payload) != size:
                raise EOFError('incomplete fixture')
            time.sleep(max(0, start + index / 20 - time.monotonic()))
            stream.write(prefix)
            stream.write(payload)
            stream.flush()
            index += 1


if __name__ == '__main__':
    send(sys.argv[1])
