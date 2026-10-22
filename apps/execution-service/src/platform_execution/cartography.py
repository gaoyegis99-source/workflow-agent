"""Independent standard-library validation for trusted PNG cartographic products."""

import binascii
import hashlib
import struct
import zlib

from platform_contracts.service import ensure

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PALETTES = {
    "blue": ((76, 120, 168), (36, 74, 107)),
    "green": ((89, 161, 79), (47, 102, 48)),
    "orange": ((242, 142, 43), (164, 75, 0)),
}


def _paeth(a, b, c):
    value = a + b - c
    da, db, dc = abs(value - a), abs(value - b), abs(value - c)
    return a if da <= db and da <= dc else b if db <= dc else c


def _scanlines(raw, width, height, channels):
    stride = width * channels
    ensure(
        len(raw) == height * (stride + 1),
        "INVALID_BINDING",
        "PNG decompressed size differs",
    )
    rows, previous, offset = [], bytearray(stride), 0
    for _ in range(height):
        kind, offset = raw[offset], offset + 1
        ensure(kind <= 4, "INVALID_BINDING", "PNG filter is unsupported")
        encoded = raw[offset : offset + stride]
        offset += stride
        row = bytearray(stride)
        for index, value in enumerate(encoded):
            left = row[index - channels] if index >= channels else 0
            above = previous[index]
            upper_left = previous[index - channels] if index >= channels else 0
            predictor = (
                0
                if kind == 0
                else left
                if kind == 1
                else above
                if kind == 2
                else (left + above) // 2
                if kind == 3
                else _paeth(left, above, upper_left)
            )
            row[index] = (value + predictor) & 255
        rows.append(bytes(row))
        previous = row
    return rows


def verify_png(content, palette, legend):
    ensure(
        isinstance(content, bytes)
        and len(content) <= 20 * 1024 * 1024
        and content.startswith(PNG_SIGNATURE),
        "INVALID_BINDING",
        "Invalid PNG signature or size",
    )
    offset, header, compressed, ended = len(PNG_SIGNATURE), None, bytearray(), False
    while offset < len(content):
        ensure(offset + 12 <= len(content), "INVALID_BINDING", "PNG chunk is truncated")
        length = struct.unpack(">I", content[offset : offset + 4])[0]
        chunk_type = content[offset + 4 : offset + 8]
        start, end = offset + 8, offset + 8 + length
        ensure(end + 4 <= len(content), "INVALID_BINDING", "PNG chunk exceeds input")
        data, expected_crc = content[start:end], struct.unpack(">I", content[end : end + 4])[0]
        ensure(
            binascii.crc32(chunk_type + data) & 0xFFFFFFFF == expected_crc,
            "INVALID_BINDING",
            "PNG chunk checksum differs",
        )
        if chunk_type == b"IHDR":
            ensure(header is None and length == 13, "INVALID_BINDING", "PNG header differs")
            header = struct.unpack(">IIBBBBB", data)
        elif chunk_type == b"IDAT":
            compressed.extend(data)
        elif chunk_type == b"IEND":
            ensure(length == 0, "INVALID_BINDING", "PNG end chunk differs")
            ended = True
            offset = end + 4
            break
        offset = end + 4
    ensure(
        ended and offset == len(content) and header is not None,
        "INVALID_BINDING",
        "PNG structure is incomplete",
    )
    width, height, depth, color, compression, filtering, interlace = header
    ensure(
        (width, height) == (1754, 1240)
        and depth == 8
        and color in {2, 6}
        and compression == filtering == interlace == 0,
        "INVALID_BINDING",
        "PNG raster contract differs",
    )
    channels = 3 if color == 2 else 4
    expected_size = height * (width * channels + 1)
    decoder = zlib.decompressobj()
    raw = decoder.decompress(bytes(compressed), expected_size + 1)
    ensure(
        len(raw) == expected_size and decoder.eof and not decoder.unconsumed_tail,
        "INVALID_BINDING",
        "PNG compressed raster differs",
    )
    rows = _scanlines(raw, width, height, channels)
    x0, x1 = 71, (1417 if legend else 1683)
    y0, y1 = 165, 1104
    colors = PALETTES.get(palette)
    ensure(colors is not None, "INVALID_BINDING", "PNG palette is unknown")
    colored, nonwhite = 0, 0
    for row in rows[y0:y1]:
        for index in range(x0 * channels, x1 * channels, channels):
            pixel = tuple(row[index : index + 3])
            if pixel != (255, 255, 255):
                nonwhite += 1
            if any(max(abs(pixel[i] - target[i]) for i in range(3)) <= 16 for target in colors):
                colored += 1
    ensure(
        nonwhite > 100 and colored > 0,
        "INVALID_BINDING",
        "PNG map frame is blank or lacks the trusted palette",
    )
    return {
        "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
        "size": len(content),
        "width": width,
        "height": height,
        "colored_pixels": colored,
    }
