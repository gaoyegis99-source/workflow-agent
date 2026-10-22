"""Fixed, network-free container entry point. Paths are supplied by the trusted runtime."""

import hashlib
import json
import os
import resource
import signal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from platform_operators.series import InputError, completeness, prepare
from platform_operators.trend import trend


def run(request, rows):
    groups, expected = prepare(
        rows,
        request["binding"]["selection"]["field_bindings"],
        request["parameters"],
        request["binding"]["semantics"],
    )
    operator = request["target"]["operator_id"]
    if operator == "timeseries-completeness":
        output = completeness(groups, expected)
        method = (
            "Distinct valid calendar periods; duplicate periods count all rows, including nulls."
        )
    elif operator == "statistics-linear-trend":
        output = trend(groups, request["parameters"]["min_samples"])
        method = "Intercept OLS on elapsed UTC days; year=365.2425 days; descriptive estimate only."
    else:
        raise InputError("Operator is not registered")
    return {
        "schema": "p3-result-v1",
        "operator_id": operator,
        "version_id": "1.0.0",
        "input": request["binding"]["selection"],
        "parameters": request["parameters"],
        "semantics": request["binding"]["semantics"],
        "method": method,
        "rows": output,
    }


def main():
    signal.alarm(120)
    resource.setrlimit(resource.RLIMIT_FSIZE, (100 * 1024 * 1024, 100 * 1024 * 1024))
    source, output = Path("/input"), Path("/output")
    try:
        marker = os.open(output / "started.marker", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(marker)
        request = json.loads((source / "request.json").read_text())
        content = (source / "observations.parquet").read_bytes()
        if (
            "sha256:" + hashlib.sha256(content).hexdigest()
            != request["binding"]["representation"]["digest"]
        ):
            raise InputError("Input digest mismatch")
        rows = pq.read_table(pa.BufferReader(content)).to_pylist()
        result = run(request, rows)
        (output / "result.json").write_text(json.dumps(result, allow_nan=False, ensure_ascii=False))
        pq.write_table(pa.Table.from_pylist(result["rows"]), output / "result.parquet")
        (output / "method.json").write_text(
            json.dumps({k: v for k, v in result.items() if k != "rows"}, ensure_ascii=False)
        )
        for path in output.iterdir():
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
    except (InputError, ValueError, KeyError, pa.ArrowException) as exc:
        (output / "error.json").write_text(
            json.dumps({"code": "INPUT_ERROR", "category": type(exc).__name__})
        )
        raise SystemExit(2) from None
