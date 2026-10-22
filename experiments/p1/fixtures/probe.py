"""Trusted fixture only: emit identity, permit a controlled delay/failure, do tiny work."""

import json
import os
import sys
import time
from pathlib import Path

run_id, step_path, ordinal, value, delay, fail_once, started_file = sys.argv[1:]
if started_file != "none":
    Path(started_file).write_text(json.dumps({"pid": os.getpid(), "run_id": run_id}))
if fail_once != "none":
    try:
        fd = os.open(fail_once, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        os.close(fd)
        sys.exit(17)
time.sleep(int(delay))
print(
    json.dumps(
        {
            "run_id": run_id,
            "step_path": step_path,
            "scatter_index": int(ordinal),
            "invocation_id": f"{run_id}/{step_path}/{ordinal}",
            "value": int(value),
            "result": int(value) * 2,
        }
    )
)
