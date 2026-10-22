import binascii
import struct

import pytest
from platform_contracts.service import ServiceFault
from platform_execution.cartography import verify_png


def replace_chunk(content, name, transform):
    offset = 8
    result = bytearray(content[:8])
    while offset < len(content):
        length = struct.unpack(">I", content[offset : offset + 4])[0]
        kind = content[offset + 4 : offset + 8]
        data = content[offset + 8 : offset + 8 + length]
        if kind == name:
            data = transform(data)
        result.extend(struct.pack(">I", len(data)))
        result.extend(kind)
        result.extend(data)
        result.extend(struct.pack(">I", binascii.crc32(kind + data) & 0xFFFFFFFF))
        offset += 12 + length
    return bytes(result)


def test_png_verifier_rejects_signature_and_header():
    with pytest.raises(ServiceFault):
        verify_png(b"not-png", "blue", True)

    # Minimal structural input reaches the exact dimension check.
    signature = b"\x89PNG\r\n\x1a\n"
    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    chunks = []
    for kind, data in [(b"IHDR", header), (b"IEND", b"")]:
        chunks.append(
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", binascii.crc32(kind + data) & 0xFFFFFFFF)
        )
    with pytest.raises(ServiceFault):
        verify_png(signature + b"".join(chunks), "blue", True)
