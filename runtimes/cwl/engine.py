"""Trusted engine supervisor. The mounted configuration is written by Execution."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

try:
    from engine_profile import validate as validate_runtime_profile
except ModuleNotFoundError:  # Repository tests import this file outside the runtime image.
    from platform_contracts.engine_runtime import validate as validate_runtime_profile

ROOT = Path("/run/work")
CONFIG = Path("/run/config.json")
GRACE_SECONDS = 20
FORCE_WAIT_SECONDS = 5


def save(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _signal_group(pid, signum):
    try:
        os.killpg(pid, signum)
    except ProcessLookupError:
        pass


def run_supervised(
    command,
    output,
    *,
    grace_seconds=GRACE_SECONDS,
    force_wait_seconds=FORCE_WAIT_SECONDS,
):
    """Run the Toil leader in its own group and bound graceful shutdown."""
    process = None
    requested_signal = None
    graceful_deadline = None
    force_deadline = None
    forced = False

    def terminate(signum, _frame):
        nonlocal requested_signal, graceful_deadline
        if requested_signal is not None:
            return
        requested_signal = signum
        graceful_deadline = time.monotonic() + grace_seconds
        if process is not None:
            _signal_group(process.pid, signum)

    previous = {
        signum: signal.signal(signum, terminate) for signum in (signal.SIGTERM, signal.SIGINT)
    }
    try:
        process = subprocess.Popen(command, stdout=output, start_new_session=True)
        if requested_signal is not None:
            _signal_group(process.pid, requested_signal)
        while True:
            try:
                returncode = process.wait(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                now = time.monotonic()
                if (
                    requested_signal is not None
                    and not forced
                    and graceful_deadline is not None
                    and now >= graceful_deadline
                ):
                    _signal_group(process.pid, signal.SIGKILL)
                    forced = True
                    force_deadline = now + force_wait_seconds
                elif forced and force_deadline is not None and now >= force_deadline:
                    raise RuntimeError("Toil process group did not stop after SIGKILL") from None
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return {
        "returncode": returncode,
        "termination_signal": (
            signal.Signals(requested_signal).name if requested_signal is not None else None
        ),
        "forced": forced,
    }


def toil_command(config):
    """Build arguments only from an exact trusted profile and fixed paths."""
    runtime_profile = validate_runtime_profile(config["engine_runtime_profile"])
    command = [
        "toil-cwl-runner",
        "--jobStore",
        str(ROOT / "jobstore"),
        "--workDir",
        str(ROOT / "work"),
        "--outdir",
        str(ROOT / "results"),
        "--no-container",
        "--clean",
        "never",
        "--retryCount",
        str(runtime_profile["retry_count"]),
        "--disableChaining",
        str(runtime_profile["disable_chaining"]).lower(),
        "--maxCores",
        str(runtime_profile["max_cores"]),
        "--maxJobs",
        str(runtime_profile["max_jobs"]),
        "--maxLocalJobs",
        str(runtime_profile["max_local_jobs"]),
        "--defaultMemory",
        runtime_profile["default_memory"],
        "--publishWorkflowMetrics",
        "no",
    ]
    if config["restart"]:
        if not (ROOT / "jobstore").is_dir():
            raise RuntimeError("Original JobStore is missing")
        command.append("--restart")
    compiled = config.get("engine_package", config["compiled"])
    return command + [str(ROOT / "package" / compiled["entrypoint"]), str(ROOT / "inputs.json")]


def main():
    config = json.loads(CONFIG.read_text())
    compiled = config.get("engine_package", config["compiled"])
    if sys.argv[1:] == ["validate"]:
        result = subprocess.run(
            ["cwltool", "--validate", str(ROOT / "package" / compiled["entrypoint"])], check=False
        )
        raise SystemExit(result.returncode)
    epoch = config["epoch"]
    command = toil_command(config)
    (ROOT / "work").mkdir(exist_ok=True)
    with (ROOT / f"result-{epoch}.json").open("w") as output:
        result = run_supervised(command, output)
        output.flush()
        os.fsync(output.fileno())
    save(
        ROOT / f"exit-{epoch}.json",
        {
            "schema_version": 1,
            "epoch": epoch,
            **result,
        },
    )
    returncode = result["returncode"]
    raise SystemExit(returncode if returncode >= 0 else 128 - returncode)


if __name__ == "__main__":
    main()
