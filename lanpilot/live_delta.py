"""Lossless live JPEG patches over an explicitly confirmed viewer frame."""

import base64
import hashlib
import struct
import zlib

MAX_JPEG_BASE64 = 1_400_000
MAX_JPEG_BYTES = MAX_JPEG_BASE64 // 4 * 3
BLOCK_SIZE = 8192
MAX_BLOCK = BLOCK_SIZE + 64
HEADER = struct.Struct('!I32s')  # JPEG length, SHA-256 of the reconstructed JPEG
BLOCK_LENGTH = struct.Struct('!H')


class DeltaError(ValueError):
    """A full frame is needed to resume the viewer stream."""


def decode_jpeg(encoded):
    if not isinstance(encoded, str) or len(encoded) > MAX_JPEG_BASE64:
        raise ValueError('画面过大')
    raw = base64.b64decode(encoded, validate=True)
    if not has_jpeg_markers(raw):
        raise ValueError('无效画面格式')
    return raw


def has_jpeg_markers(raw):
    return len(raw) >= 4 and raw.startswith(b'\xff\xd8') and raw.endswith(b'\xff\xd9')


def dictionary(base, offset):
    return base[max(0, offset - 12000):offset + 20000]


def encode_delta(base_encoded, jpeg_encoded, base_seq):
    # Both representations are base64 strings in the same JSON frame. The
    # patch adds a comma and the "delta_after" field with its integer value.
    # Require at least 5% wire savings to justify the extra codec work.
    max_encoded = len(jpeg_encoded) * 95 // 100 - len(',"delta_after":') - len(str(base_seq)) - 1
    budget = max_encoded // 4 * 3
    if budget <= HEADER.size:
        return None

    base, jpeg = decode_jpeg(base_encoded), decode_jpeg(jpeg_encoded)
    patch = bytearray(HEADER.pack(len(jpeg), hashlib.sha256(jpeg).digest()))
    for offset in range(0, len(jpeg), BLOCK_SIZE):
        compressor = zlib.compressobj(1, zlib.DEFLATED, -15, zdict=dictionary(base, offset))
        block = compressor.compress(jpeg[offset:offset + BLOCK_SIZE]) + compressor.flush()
        if len(patch) + BLOCK_LENGTH.size + len(block) > budget:
            return None
        patch.extend(BLOCK_LENGTH.pack(len(block)))
        patch.extend(block)
    return base64.b64encode(patch).decode('ascii')


def decode_delta(base_encoded, delta_encoded):
    try:
        base = decode_jpeg(base_encoded)
        if not isinstance(delta_encoded, str) or len(delta_encoded) > MAX_JPEG_BASE64:
            raise ValueError('invalid patch size')
        patch = base64.b64decode(delta_encoded, validate=True)
        jpeg_length, digest = HEADER.unpack_from(patch)
        if not 4 <= jpeg_length <= MAX_JPEG_BYTES:
            raise ValueError('invalid JPEG size')

        jpeg = bytearray()
        cursor = HEADER.size
        for offset in range(0, jpeg_length, BLOCK_SIZE):
            block_length, = BLOCK_LENGTH.unpack_from(patch, cursor)
            cursor += BLOCK_LENGTH.size
            end = cursor + block_length
            if not 0 < block_length <= MAX_BLOCK or end > len(patch):
                raise ValueError('invalid block size')
            expected = min(BLOCK_SIZE, jpeg_length - offset)
            decompressor = zlib.decompressobj(-15, zdict=dictionary(base, offset))
            block = decompressor.decompress(patch[cursor:end], expected + 1)
            if (len(block) != expected or not decompressor.eof or
                    decompressor.unused_data or decompressor.unconsumed_tail):
                raise ValueError('invalid block output')
            jpeg.extend(block)
            cursor = end

        if cursor != len(patch) or not has_jpeg_markers(jpeg) or hashlib.sha256(jpeg).digest() != digest:
            raise ValueError('invalid reconstructed JPEG')
        return base64.b64encode(jpeg).decode('ascii')
    except (ValueError, struct.error, zlib.error) as error:
        raise DeltaError('画面差分无效，需要重新获取完整画面') from error
