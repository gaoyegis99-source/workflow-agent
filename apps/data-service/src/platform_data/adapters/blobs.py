"""Immutable content-addressed files; visibility is exclusively owned by PostgreSQL."""

import hashlib
import os
import tempfile
from pathlib import Path

from platform_contracts.service import ensure


class Blobs:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, digest):
        ensure(
            isinstance(digest, str)
            and len(digest) == 71
            and digest.startswith("sha256:")
            and all(c in "0123456789abcdef" for c in digest[7:]),
            "INVALID_BINDING",
            "Invalid content digest",
        )
        return self.root / digest[7:]

    def store(self, content):
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        fd, temporary = tempfile.mkstemp(dir=self.root)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, self.path(digest))
            except FileExistsError:
                ensure(
                    self.path(digest).read_bytes() == content,
                    "INVALID_BINDING",
                    "Stored content digest mismatch",
                )
            directory = os.open(self.root, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            os.unlink(temporary)
        return digest

    def read(self, digest):
        content = self.path(digest).read_bytes()
        ensure(
            "sha256:" + hashlib.sha256(content).hexdigest() == digest,
            "INVALID_BINDING",
            "Stored content digest mismatch",
        )
        return content
