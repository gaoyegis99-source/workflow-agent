"""P1-only Toil experiment adapter. No Platform business state or public API.

The sidecars locate a probe process; they are not a production execution ledger.
Only the trusted checked-in fixture profile is accepted by the suite.
"""

import argparse
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
BIN = HERE / "toil/.venv/bin"


def save(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def identity(pid: int) -> dict | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return {
            "pid": pid,
            "start_ticks": fields[19],
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        }
    except (FileNotFoundError, ProcessLookupError):
        return None


def alive(record: dict | None) -> bool:
    return bool(record and identity(record["pid"]) == record)


def compile_fixture(destination: Path) -> tuple[Path, dict]:
    """Clone static references and name each tool by its full static call path.

    This deliberately only handles our P1 fixture, not general/dynamic CWL.
    Toil 9.5 can shorten parent paths inside nested scatters; unique tool IDs
    preserve static path identity without modifying engine internals.
    """
    destination.mkdir(parents=True)
    mapping = {}

    def compile_one(source: Path, path: tuple[str, ...]) -> str:
        document = json.loads(source.read_text())
        identifier = "v7_" + ("_".join(path) or "root")
        document["id"] = identifier
        if document["class"] == "Workflow":
            for name, step in document["steps"].items():
                step["run"] = compile_one(source.parent / step["run"], (*path, name))
        else:
            mapping[identifier] = "/".join(path)
        filename = identifier + ".cwl"
        save(destination / filename, document)
        return filename

    entry = compile_one(HERE / "fixtures/nested.cwl", ())
    (destination / "probe.py").write_bytes((HERE / "fixtures/probe.py").read_bytes())
    save(destination / "step-map.json", mapping)
    return destination / entry, mapping


def query(directory: Path) -> dict:
    intent = directory / "intent.json"
    if not intent.exists():
        return {"state": "MISSING"}
    config = json.loads(intent.read_text())
    attempt = config["attempt"]
    done = directory / f"exit-{attempt}.json"
    engine_file = directory / f"engine-{attempt}.json"
    engine = json.loads(engine_file.read_text()) if engine_file.exists() else None
    if alive(engine):
        state = "CANCELLING" if (directory / "cancel.json").exists() else "RUNNING"
    elif done.exists():
        code = json.loads(done.read_text())["returncode"]
        state = "SUCCEEDED" if code == 0 else "FAILED"
        if (directory / "cancel.json").exists():
            state = "CANCELLED"
    else:
        supervisor = config.get("supervisor")
        state = "STARTING" if alive(supervisor) else "UNKNOWN"
    return {"state": state, "attempt": attempt, "engine": engine, "job_store": config["store"]}


def submit(directory: Path, workflow: Path, inputs: dict, *, restart: bool = False) -> dict:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "adapter.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        intent = directory / "intent.json"
        digest = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
        if intent.exists():
            config = json.loads(intent.read_text())
            if config["inputs_digest"] != digest or config["workflow"] != str(workflow):
                raise ValueError("Existing probe identity has different inputs/workflow")
            if not restart:
                return query(directory)
            if query(directory)["state"] != "FAILED" or alive(config.get("supervisor")):
                raise ValueError("Only a confirmed failed and stopped probe may restart")
            config["attempt"] += 1
        else:
            if restart:
                raise ValueError("Cannot restart a missing probe")
            config = {
                "workflow": str(workflow),
                "inputs_digest": digest,
                "store": str(directory / "job-store"),
                "attempt": 1,
            }
            save(directory / "inputs.json", inputs)
        # An interrupted launch stays UNKNOWN; never silently start another leader.
        config.pop("supervisor", None)
        save(intent, config)
        with (directory / "supervisor.log").open("ab") as log:
            process = subprocess.Popen(
                [str(BIN / "python"), str(Path(__file__).resolve()), "_leader", str(directory)],
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        config["supervisor"] = identity(process.pid)
        save(intent, config)
        return query(directory)


def leader(directory: Path) -> None:
    # Wait for submit to finish persisting the supervisor identity before reading intent.
    with (directory / "adapter.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        config = json.loads((directory / "intent.json").read_text())
    attempt = config["attempt"]
    work = directory / "work"
    work.mkdir(exist_ok=True)
    command = [
        str(BIN / "toil-cwl-runner"),
        "--jobStore",
        config["store"],
        "--outdir",
        str(directory / "outputs"),
        "--workDir",
        str(work),
        "--clean",
        "never",
        "--retryCount",
        "0",
        "--maxCores",
        "2",
        "--maxLocalJobs",
        "2",
        "--maxJobs",
        "2",
        "--no-container",
        "--publishWorkflowMetrics",
        "no",
        "--logLevel",
        "WARNING",
        "--writeMessages",
        str(directory / f"messages-{attempt}.log"),
    ]
    if attempt > 1:
        command.append("--restart")
    command += [config["workflow"], str(directory / "inputs.json")]
    save(directory / f"command-{attempt}.json", {"argv": command})
    env = os.environ.copy()
    env["PATH"] = str(BIN) + os.pathsep + env["PATH"]
    env.pop("PYTHONPATH", None)
    with (
        (directory / f"result-{attempt}.json").open("w") as out,
        (directory / f"stderr-{attempt}.log").open("w") as err,
    ):
        process = subprocess.Popen(command, stdout=out, stderr=err, env=env)
        save(directory / f"engine-{attempt}.json", identity(process.pid))
        code = process.wait()
    save(directory / f"exit-{attempt}.json", {"returncode": code})


def cancel(directory: Path) -> dict:
    with (directory / "adapter.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = query(directory)
        if state["state"] == "CANCELLED":
            return state
        if state["state"] != "RUNNING" or not alive(state["engine"]):
            raise ValueError("Cannot cancel without a verified live engine identity")
        save(directory / "cancel.json", {"engine": state["engine"]})
        os.kill(state["engine"]["pid"], signal.SIGTERM)
    return query(directory)


def wait_for(directory: Path, states: set[str], *, timeout: float = 120) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = query(directory)
        if state["state"] in states:
            return state
        time.sleep(0.2)
    raise TimeoutError(f"Probe did not reach {states}: {query(directory)}")


def jobs(directory: Path, attempt: int) -> list[dict]:
    from toil.bus import replay_message_bus

    if not (directory / f"messages-{attempt}.log").exists():
        return []
    return [
        asdict(job) for job in replay_message_bus(directory / f"messages-{attempt}.log").values()
    ]


def suite(root: Path) -> dict:
    import psutil

    root.mkdir(parents=True)
    workflow, mapping = compile_fixture(root / "compiled")
    inputs = {
        "script": {"class": "File", "location": (workflow.parent / "probe.py").as_uri()},
        "run_id": "p1-identity",
        "step_path": "outer/scatter",
        "ordinals": [0, 1],
        "values": [7, 7],
        "delay": 0,
        "fail_once": "none",
    }
    checks = []
    active = []
    try:
        run = root / "identity"
        submit(run, workflow, inputs)
        active.append(run)
        first = wait_for(run, {"RUNNING", "FAILED"})
        assert first["state"] == "RUNNING", first
        # A new client process reattaches solely from the durable handle.
        reattached = json.loads(
            subprocess.check_output(
                [sys.executable, str(Path(__file__).resolve()), "query", str(run)], text=True
            )
        )
        assert reattached["engine"] == first["engine"]
        replay = submit(run, workflow, inputs)
        assert replay["engine"] == first["engine"]
        assert wait_for(run, {"SUCCEEDED", "FAILED"})["state"] == "SUCCEEDED"
        output = json.loads((run / "result-1.json").read_text())
        records = [json.loads(Path(item["path"]).read_text()) for item in output["records"]]
        assert [record["result"] for record in records] == [14, 14]
        assert len({record["invocation_id"] for record in records}) == 2
        engine_jobs = [job for job in jobs(run, 1) if job["name"].endswith("v7_outer_scatter")]
        assert len(engine_jobs) == 2
        assert {job["name"].split(".")[-2] for job in engine_jobs} == {"0", "1"}
        assert len({job["job_store_id"] for job in engine_jobs}) == 2
        checks.append(
            {
                "check": "submit_query_relocate_duplicate_scatter",
                "status": "PASS",
                "handle": first,
                "records": records,
                "engine_jobs": engine_jobs,
                "step_map": mapping,
            }
        )

        failed = root / "recovery"
        retry_inputs = dict(
            inputs,
            ordinals=[0],
            values=[7],
            run_id="p1-retry",
            fail_once=str(root / "fail-once.marker"),
        )
        submit(failed, workflow, retry_inputs)
        active.append(failed)
        assert wait_for(failed, {"FAILED", "SUCCEEDED"})["state"] == "FAILED"
        # Wait for the small supervisor to exit as well as the engine.
        deadline = time.monotonic() + 10
        while alive(json.loads((failed / "intent.json").read_text()).get("supervisor")):
            assert time.monotonic() < deadline
            time.sleep(0.1)
        previous = [job for job in jobs(failed, 1) if job["name"].endswith("v7_outer_scatter")]
        assert len(previous) == 1 and previous[0]["exit_code"] != 0
        submit(failed, workflow, retry_inputs, restart=True)
        assert wait_for(failed, {"SUCCEEDED", "FAILED"})["state"] == "SUCCEEDED"
        resumed = [job for job in jobs(failed, 2) if job["name"].endswith("v7_outer_scatter")]
        assert [j["job_store_id"] for j in previous] == [j["job_store_id"] for j in resumed]
        checks.append(
            {
                "check": "failed_attempt_restart_same_jobstore",
                "status": "PASS",
                "before": previous,
                "after": resumed,
                "attempts": [1, 2],
            }
        )

        cancelled = root / "cancel"
        started = root / "cancel-started.json"
        submit(
            cancelled,
            workflow,
            dict(inputs, delay=60, ordinals=[0], values=[7], started_file=str(started)),
        )
        active.append(cancelled)
        running = wait_for(cancelled, {"RUNNING"})
        deadline = time.monotonic() + 30
        while not started.exists():
            assert time.monotonic() < deadline
            time.sleep(0.2)
        fixture_pid = json.loads(started.read_text())["pid"]
        descendants = psutil.Process(running["engine"]["pid"]).children(recursive=True)
        saved_children = [identity(child.pid) for child in descendants]
        assert any(child and child["pid"] == fixture_pid for child in saved_children)
        cancel(cancelled)
        assert wait_for(cancelled, {"CANCELLED"}, timeout=30)["state"] == "CANCELLED"
        deadline = time.monotonic() + 10
        while any(alive(child) for child in saved_children):
            assert time.monotonic() < deadline, "A cancelled worker is still alive"
            time.sleep(0.1)
        assert not list((cancelled / "outputs").glob("probe.json*"))
        assert cancel(cancelled)["state"] == "CANCELLED"
        checks.append(
            {
                "check": "cancel_live_worker_and_repeat",
                "status": "PASS",
                "child_count": len(saved_children),
                "children_stopped": True,
            }
        )
        return {"candidate": "Toil", "version": "9.5.0", "status": "PASS", "checks": checks}
    finally:
        for run in active:
            current = query(run)
            if current["state"] == "RUNNING":
                cancel(run)
                wait_for(run, {"CANCELLED"}, timeout=30)


def main() -> None:
    if sys.flags.optimize:
        raise RuntimeError("Probe assertions must not be disabled")
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["suite", "query", "_leader"])
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    directory = args.directory.resolve()
    if args.command == "_leader":
        leader(directory)
    elif args.command == "query":
        print(json.dumps(query(directory)))
    else:
        result = suite(directory)
        save(directory / "report.json", result)
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
