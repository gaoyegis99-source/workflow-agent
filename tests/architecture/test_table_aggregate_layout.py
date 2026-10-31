"""Regression guard for the no-schema-change declaration split."""

from __future__ import annotations

import hashlib
import json

import sqlalchemy as sa
from platform_infrastructure.tables import metadata
from sqlalchemy.dialects import postgresql

_DIALECT = postgresql.dialect()
_EXPECTED_TABLE_COUNT = 77
_EXPECTED_SCHEMA_SHA256 = "2befcb19d3af061f7f472d353e3687b607385a3f69d8f86ade8400eab64b2168"


def _render(value) -> str | None:
    if value is None:
        return None
    try:
        return str(
            value.compile(
                dialect=_DIALECT,
                compile_kwargs={"literal_binds": True},
            )
        )
    except Exception:  # SQLAlchemy values without a SQL compiler are still stable here.
        return str(value)


def _constraint_shape(constraint) -> dict[str, object]:
    shape: dict[str, object] = {"kind": type(constraint).__name__, "name": constraint.name}
    if isinstance(constraint, (sa.PrimaryKeyConstraint, sa.UniqueConstraint)):
        shape["columns"] = tuple(column.name for column in constraint.columns)
    elif isinstance(constraint, sa.CheckConstraint):
        shape["columns"] = tuple(column.name for column in constraint.columns)
        shape["sqltext"] = _render(constraint.sqltext)
    elif isinstance(constraint, sa.ForeignKeyConstraint):
        shape.update(
            columns=tuple(column.name for column in constraint.columns),
            targets=tuple(item.target_fullname for item in constraint.elements),
            ondelete=constraint.ondelete,
            onupdate=constraint.onupdate,
            deferrable=constraint.deferrable,
            initially=constraint.initially,
            use_alter=constraint.use_alter,
        )
    return shape


def _index_shape(index) -> dict[str, object]:
    postgresql_options = dict(index.dialect_options["postgresql"])
    return {
        "name": index.name,
        "unique": index.unique,
        "columns": tuple(str(value) for value in index.expressions),
        "postgresql": {
            key: _render(value) if key == "where" else value
            for key, value in sorted(postgresql_options.items())
            if value is not None
        },
    }


def _table_shape(table) -> dict[str, object]:
    return {
        "name": table.name,
        "columns": [
            {
                "name": column.name,
                "type": str(column.type.compile(dialect=_DIALECT)),
                "nullable": column.nullable,
                "primary_key": column.primary_key,
                "unique": column.unique,
                "autoincrement": column.autoincrement,
                "default": _render(column.default.arg) if column.default else None,
                "server_default": (
                    _render(column.server_default.arg) if column.server_default else None
                ),
                "foreign_keys": tuple(sorted(key.target_fullname for key in column.foreign_keys)),
            }
            for column in table.columns
        ],
        "constraints": sorted(
            (_constraint_shape(constraint) for constraint in table.constraints),
            key=lambda item: json.dumps(item, sort_keys=True),
        ),
        "indexes": sorted(
            (_index_shape(index) for index in table.indexes),
            key=lambda item: json.dumps(item, sort_keys=True),
        ),
    }


def test_aggregate_declaration_split_preserves_the_exact_schema_shape():
    shape = [_table_shape(metadata.tables[name]) for name in sorted(metadata.tables)]
    payload = json.dumps(shape, ensure_ascii=True, separators=(",", ":"), sort_keys=True)

    assert len(shape) == _EXPECTED_TABLE_COUNT
    assert hashlib.sha256(payload.encode()).hexdigest() == _EXPECTED_SCHEMA_SHA256
