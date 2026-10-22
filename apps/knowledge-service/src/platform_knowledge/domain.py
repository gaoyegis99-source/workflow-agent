"""Deterministic text normalization and evidence segmentation; standard library only."""

import hashlib
import re
import unicodedata

NORMALIZATION = "utf8-nfc-lf-v1"
CHUNKING = "paragraph-window-v1"
WINDOW = 2_000
OVERLAP = 200


def sha256(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def normalize(raw: bytes) -> str:
    text = raw.decode("utf-8-sig")
    text = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    if not text.strip():
        raise ValueError("Document has no textual content")
    if "\x00" in text or any(
        character not in "\n\t\f" and ord(character) < 32 for character in text
    ):
        raise ValueError("Document contains unsupported control characters")
    return text


def _blocks(text: str):
    lines = text.splitlines(keepends=True)
    start = 0
    block_start = None
    block_end = None
    for line in lines:
        end = start + len(line)
        blank = not line.strip()
        heading = bool(re.match(r"^ {0,3}#{1,6}(?:\s|$)", line))
        if blank:
            if block_start is not None:
                yield block_start, block_end
                block_start = block_end = None
        elif heading:
            if block_start is not None:
                yield block_start, block_end
            yield start, end
            block_start = block_end = None
        else:
            if block_start is None:
                block_start = start
            block_end = end
        start = end
    if block_start is not None:
        yield block_start, block_end


def segment(text: str):
    units = []
    for block_start, block_end in _blocks(text):
        cursor = block_start
        while cursor < block_end:
            end = min(cursor + WINDOW, block_end)
            exact = text[cursor:end]
            if exact.strip():
                units.append(
                    {
                        "start_char": cursor,
                        "end_char": end,
                        "text": exact,
                        "exact_text_digest": sha256(exact.encode()),
                        "prefix": text[max(0, cursor - 64) : cursor],
                        "suffix": text[end : min(len(text), end + 64)],
                    }
                )
            if end == block_end:
                break
            cursor = end - OVERLAP
    if not units:
        raise ValueError("Document has no evidence units")
    return units
