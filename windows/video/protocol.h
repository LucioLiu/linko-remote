#pragma once

#include <array>
#include <cstddef>
#include <cstdint>
#include <vector>

namespace video {
constexpr uint32_t max_payload = 2 * 1024 * 1024;
constexpr uint64_t max_lease_ms = 4000;
constexpr size_t command_size = 9;
constexpr size_t header_size = 48;
constexpr unsigned profile_high = 1;
constexpr unsigned profile_low = 2;
constexpr unsigned low_bitrate = 750000;
constexpr unsigned low_buffer_bytes = 12500;
using Bytes = std::vector<uint8_t>;
using Command = std::array<uint8_t, command_size>;

inline bool valid_deadline(uint64_t now, uint64_t deadline) {
    return deadline > now && deadline - now <= max_lease_ms;
}

inline uint64_t read_le(const uint8_t* bytes, size_t count) {
    uint64_t result = 0;
    for (size_t i = 0; i < count; ++i) result |= uint64_t(bytes[i]) << (i * 8);
    return result;
}

inline void write_le(uint8_t* bytes, uint64_t value, size_t count) {
    for (size_t i = 0; i < count; ++i) bytes[i] = uint8_t(value >> (i * 8));
}

struct Bounds {
    int32_t x, y, width, height;
    bool operator==(const Bounds& other) const {
        return x == other.x && y == other.y && width == other.width && height == other.height;
    }
};

inline std::array<uint8_t, header_size> frame_header(
    uint32_t length, uint64_t sequence, uint64_t capture_tick, uint16_t width,
    uint16_t height, bool idr, Bounds desktop) {
    std::array<uint8_t, header_size> bytes{'L', 'P', 'A', '1'};
    write_le(bytes.data() + 4, length, 4);
    write_le(bytes.data() + 8, sequence, 8);
    write_le(bytes.data() + 16, capture_tick, 8);
    write_le(bytes.data() + 24, width, 2);
    write_le(bytes.data() + 26, height, 2);
    write_le(bytes.data() + 28, idr, 4);
    write_le(bytes.data() + 32, uint32_t(desktop.x), 4);
    write_le(bytes.data() + 36, uint32_t(desktop.y), 4);
    write_le(bytes.data() + 40, desktop.width, 4);
    write_le(bytes.data() + 44, desktop.height, 4);
    return bytes;
}

enum NalFlags { slice = 1, idr = 2, sps = 4, pps = 8 };

inline unsigned nal_flags(const Bytes& bytes) {
    unsigned flags = 0;
    bool seen_start = false;
    for (size_t i = 2; i < bytes.size(); ++i) {
        if (bytes[i - 2] || bytes[i - 1] || bytes[i] != 1) {
            if (!seen_start && bytes[i - 2]) return 0;
            continue;
        }
        seen_start = true;
        if (++i == bytes.size() || (bytes[i] & 0x80)) return 0;
        switch (bytes[i] & 31) {
            case 1: flags |= slice; break;
            case 5: flags |= slice | idr; break;
            case 7: flags |= sps; break;
            case 8: flags |= pps; break;
        }
    }
    return flags;
}

// BT.601 studio range, averaging a 2x2 block for each interleaved UV pair.
inline void bgra_to_nv12(const uint8_t* source, uint8_t* target, unsigned width, unsigned height) {
    auto* uv = target + width * height;
    for (unsigned y = 0; y < height; y += 2) {
        for (unsigned x = 0; x < width; x += 2) {
            int red = 0, green = 0, blue = 0;
            for (unsigned dy = 0; dy < 2; ++dy) {
                for (unsigned dx = 0; dx < 2; ++dx) {
                    const unsigned pixel = (y + dy) * width + x + dx;
                    const int b = source[4 * pixel];
                    const int g = source[4 * pixel + 1];
                    const int r = source[4 * pixel + 2];
                    target[pixel] = uint8_t(16 + (66*r + 129*g + 25*b + 128) / 256);
                    red += r;
                    green += g;
                    blue += b;
                }
            }
            uv[y / 2 * width + x] = uint8_t((131584 - 38*red - 74*green + 112*blue) / 1024);
            uv[y / 2 * width + x + 1] = uint8_t((131584 + 112*red - 94*green - 18*blue) / 1024);
        }
    }
}
}  // namespace video
