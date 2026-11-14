"""A small test PNG without Pillow: two filled squares on grey, 256 x 192 (for smoke.sh)."""

import struct
import sys
import zlib

W, H = 256, 192


def pixel(x: int, y: int) -> bytes:
    if 40 <= x < 120 and 40 <= y < 120:
        return b"\xd0\x20\x20"
    if 150 <= x < 220 and 60 <= y < 160:
        return b"\x20\x40\xd0"
    return b"\x80\x80\x80"


def chunk(kind: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)


rows = b"".join(b"\x00" + b"".join(pixel(x, y) for x in range(W)) for y in range(H))
png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", W, H, 8, 2, 0, 0, 0)) + \
    chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b"")
with open(sys.argv[1], "wb") as f:
    f.write(png)
