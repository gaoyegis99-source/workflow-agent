"""Shared SQLAlchemy repository primitives."""

import hashlib
import json

import sqlalchemy as sa
from platform_core.domain.control import require


def digest_record(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def one(connection, query, *, required=True):
    row = connection.execute(query).mappings().first()
    if required:
        require(row is not None, "RESOURCE_NOT_FOUND", "Resource not found", 404)
    return dict(row) if row else None


class Repository:
    def __init__(self, connection):
        self.c = connection

    def now(self):
        return self.c.scalar(sa.select(sa.func.clock_timestamp()))
