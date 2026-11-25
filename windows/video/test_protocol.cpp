#include "protocol.h"

#include <cassert>

using namespace video;

int main() {
    assert(!valid_deadline(100, 99));
    assert(!valid_deadline(100, 100));
    assert(valid_deadline(100, 101));
    assert(valid_deadline(100, 4100));
    assert(!valid_deadline(100, 4101));
    assert(!valid_deadline(UINT64_MAX, 10));

    Command command{'R'};
    write_le(command.data() + 1, 0xfedcba9876543210, 8);
    assert(command[1] == 0x10 && command[8] == 0xfe);
    assert(read_le(command.data() + 1, 8) == 0xfedcba9876543210);
    const auto header = frame_header(0x10000, 257, 768, 1280, 720, true, {-1920, 0, 3840, 1080});
    const std::array<uint8_t, header_size> expected{
        'L','P','A','1', 0,0,1,0, 1,1,0,0,0,0,0,0, 0,3,0,0,0,0,0,0,
        0,5,208,2, 1,0,0,0, 128,248,255,255, 0,0,0,0, 0,15,0,0, 56,4,0,0
    };
    assert(header == expected);

    assert(nal_flags({0,0,0,1,0x67,9,0,0,1,0x68,9,0,0,1,0x65,9})
           == (sps | pps | idr | slice));
    assert(nal_flags({0,0,1,0x41,0,0,3,1}) == slice);
    assert(!nal_flags({5,0,0,1,0x65}));
    assert(!nal_flags({0,0,1,0x65,0,0,1}));
    assert(!nal_flags({0,0,1,0xe5}));
    assert(!nal_flags({0,0,0,2,0x65,9}));

    const uint8_t bgra[]{0,0,0,255, 255,255,255,255, 0,0,255,255, 255,0,0,255};
    uint8_t nv12[6]{};
    bgra_to_nv12(bgra, nv12, 2, 2);
    assert(nv12[0] == 16 && nv12[1] == 235 && nv12[2] == 82 && nv12[3] == 41);
    assert(nv12[4] == 146 && nv12[5] == 151);
}
