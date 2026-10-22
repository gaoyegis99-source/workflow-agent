"""Trusted Decimal checker container: read-only inputs and report-only output."""

import hashlib
import json
import os
import resource
import signal
from pathlib import Path

from platform_checkers.capabilities import check as check_capability
from platform_checkers.check import check
from platform_checkers.open_science import check as check_open_science


def main():
    signal.alarm(120)
    resource.setrlimit(resource.RLIMIT_FSIZE, (100 * 1024 * 1024, 100 * 1024 * 1024))
    source, output = Path("/input"), Path("/output")
    marker = os.open(output / "started.marker", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(marker)
    request = json.loads((source / "request.json").read_text())
    if request.get("checker_run") is not None:
        report = check_capability(request, source)
        with (output / "check.json").open("w") as stream:
            json.dump(report, stream, allow_nan=False, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        return
    if request.get("acceptance_spec", {}).get("profile") == "open-science-v1":
        report = check_open_science(request)
        with (output / "check.json").open("w") as stream:
            json.dump(report, stream, allow_nan=False, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        return
    content = (source / "observations.parquet").read_bytes()
    if (
        "sha256:" + hashlib.sha256(content).hexdigest()
        != request["binding"]["representation"]["digest"]
    ):
        raise SystemExit(2)
    import pyarrow.parquet as pq

    observations = pq.read_table(source / "observations.parquet").to_pylist()
    outputs = []
    for index, artifact in enumerate(request["artifacts"]):
        for rep in artifact["manifest"]["representations"]:
            extension = {"result": "json", "table": "parquet", "method": "method.json"}[
                rep["representation_id"]
            ]
            content = (source / f"candidate-{index}.{extension}").read_bytes()
            if "sha256:" + hashlib.sha256(content).hexdigest() != rep["digest"]:
                raise SystemExit(2)
        outputs.append(
            {
                "result": json.loads((source / f"candidate-{index}.json").read_text()),
                "table": pq.read_table(source / f"candidate-{index}.parquet").to_pylist(),
                "method": json.loads((source / f"candidate-{index}.method.json").read_text()),
            }
        )
    report = check(request, observations, outputs)
    with (output / "check.json").open("w") as stream:
        json.dump(report, stream, allow_nan=False, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
