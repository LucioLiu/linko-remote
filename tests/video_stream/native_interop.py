"""Cross-check the Windows sender's framing with the Mac native decoder.

Uses the sender's C++ header writer and Baseline 4:2:0 fixture access units.
The payloads here come from libx264, not a Windows desktop or Media Foundation.
"""
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'lanpilot/mac/video/tests'))
from verify import encode_scroll, reference

HEADER = struct.Struct('<4sIQQHHIiiII')
LIMIT = 2 * 1024 * 1024


def adapt(stream):
    result = bytearray()
    previous = 0
    waiting = True
    while header := stream.read(HEADER.size):
        if len(header) != HEADER.size:
            raise ValueError('truncated header')
        magic, length, seq, tick, width, height, flags, x, y, w, h = HEADER.unpack(header)
        if (magic != b'LPA1' or not 0 < length <= LIMIT or flags not in (0, 1)
                or not 0 < width <= w or not 0 < height <= h or seq <= previous):
            raise ValueError('invalid frame')
        data = stream.read(length)
        if len(data) != length:
            raise ValueError('truncated payload')
        if seq != previous + 1:
            result.extend(bytes(4))
            waiting = True
        previous = seq
        if waiting and not flags:
            continue
        waiting = False
        result.extend(struct.pack('!I', length))
        result.extend(data)
    return result


def main():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        writer = tmp / 'header'
        source = tmp / 'header.cpp'
        source.write_text('''#include "protocol.h"
#include <cstdio>
int main() {
    unsigned n, seq, idr;
    while (std::scanf("%u%u%u", &n, &seq, &idr) == 3) {
        const auto h = video::frame_header(n, seq, seq * 33, 1280, 720, idr, {0,0,1920,1080});
        if (std::fwrite(h.data(),1,h.size(),stdout) != h.size()) return 1;
    }
}
''')
        subprocess.run(['clang++','-std=c++17','-I',str(ROOT/'windows/video'),str(source),'-o',str(writer)], check=True)
        decoder = tmp/'decode'
        subprocess.run(['sh',str(ROOT/'lanpilot/mac/video/build.sh'),str(decoder)],check=True)
        packets = encode_scroll(1280,720,65,profile='baseline')
        expected = reference(packets)
        rows = ''.join(f'{len(bytes(p))} {i+1} {int(p.is_keyframe)}\n' for i,p in enumerate(packets))
        headers = subprocess.check_output([writer],input=rows.encode())
        assert len(headers) == HEADER.size * len(packets)
        for skip in (None, 9):
            wire = b''.join(headers[i*48:(i+1)*48]+bytes(p) for i,p in enumerate(packets) if i != skip)
            output = subprocess.check_output([decoder],input=adapt(io.BytesIO(wire)))
            frames = [v for v in map(json.loads,output.splitlines()) if v['kind']=='frame']
            wanted = expected if skip is None else expected[:skip]+expected[30:]
            assert [v['sha256'] for v in frames] == wanted
            print(json.dumps({'omitted_frame_index':skip,'verified_frames':len(frames),
                              'all_hardware':all(v['hardware'] is True for v in frames)}))


if __name__ == '__main__':
    main()
