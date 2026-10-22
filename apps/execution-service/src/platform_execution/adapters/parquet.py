"""PyArrow-backed adapter for inspecting already bounded Parquet outputs."""

from __future__ import annotations

import pyarrow.parquet as pq


class ParquetInspector:
    @staticmethod
    def inspect(path, *, rows: bool = False) -> dict:
        table = pq.read_table(path)
        return {
            "schema": [(field.name, str(field.type)) for field in table.schema],
            "row_count": table.num_rows,
            "null_counts": {field.name: table[field.name].null_count for field in table.schema},
            **({"rows": table.to_pylist()} if rows else {}),
        }
