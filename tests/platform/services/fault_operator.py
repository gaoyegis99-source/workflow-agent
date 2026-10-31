"""Test image only: corrupt the computed slope consistently in both output representations."""

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
from platform_operators.cli import main

main()
output = Path("/output")
result = json.loads((output / "result.json").read_text())
if result["operator_id"] == "statistics-linear-trend":
    result["rows"][0]["slope_per_day"] += 1.0
    (output / "result.json").write_text(json.dumps(result))
    pq.write_table(pa.Table.from_pylist(result["rows"]), output / "result.parquet")
