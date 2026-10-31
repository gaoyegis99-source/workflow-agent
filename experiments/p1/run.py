"""Run bounded real component probes and write a reproducible P1 decision record."""

import argparse
import hashlib
import json
import os
import platform
import signal
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from gate import select_engine

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.lib.runtime_layout import P1_RUNTIME_ROOT  # noqa: E402
from scripts.maintenance.project_environment import docker_executable  # noqa: E402


def run_probe(command: list[str], directory: Path, log: Path, *, candidate: str) -> dict:
    timed_out = False
    with log.open("w") as output:
        process = subprocess.Popen(command, stdout=output, stderr=subprocess.STDOUT)
        try:
            code = process.wait(timeout=480)
        except subprocess.TimeoutExpired:
            timed_out = True
            # Let each probe's finally blocks stop its own services and workers.
            process.send_signal(signal.SIGINT)
            try:
                code = process.wait(timeout=75)
            except subprocess.TimeoutExpired:
                process.kill()
                code = process.wait()
    report = directory / "report.json"
    if timed_out or not report.exists():
        return {
            "candidate": candidate,
            "status": "ERROR",
            "checks": [],
            "returncode": code,
            "timed_out": timed_out,
            "log": str(log.relative_to(ROOT)),
        }
    data = json.loads(report.read_text())
    if data.get("status") == "PASS" and code:
        raise RuntimeError(f"{candidate} returned an inconsistent exit status")
    data["returncode"] = code
    data["log"] = str(log.relative_to(ROOT))
    return data


def source_hashes() -> dict:
    paths = []
    for pattern in [
        "*.py",
        "*.ini",
        "*.yaml",
        "pins.json",
        "fixtures/*",
        "toil/pyproject.toml",
        "toil/uv.lock",
    ]:
        paths.extend(HERE.glob(pattern))
    paths += [ROOT / "scripts/checks/check_p1.sh", ROOT / "uv.lock"]
    return {
        str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(paths)
        if path.is_file()
    }


def main():
    if sys.flags.optimize:
        raise RuntimeError("Probe assertions must not be disabled")
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--record", type=Path, help="Also save the decision as a reviewable artifact"
    )
    args = parser.parse_args()
    timestamp = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
    directory = P1_RUNTIME_ROOT / ("verification-" + uuid.uuid4().hex[:12])
    directory.mkdir(parents=True)
    reports = {}
    for name, script, python in [
        (
            "Toil",
            "toil_probe.py",
            Path(
                os.environ.get(
                    "V7_P1_TOIL_PYTHON",
                    str(ROOT.parent / "runtime/toolchain/p1-toil/bin/python"),
                )
            ),
        ),
        ("Weaver", "weaver_probe.py", Path(sys.executable)),
        ("gVisor", "isolation_probe.py", Path(sys.executable)),
    ]:
        target = directory / name.lower()
        command = [str(python), str(HERE / script)]
        if name == "Toil":
            command.append("suite")
        command.append(str(target))
        print(f"P1: evaluating {name}", flush=True)
        reports[name] = run_probe(command, target, directory / f"{name}.log", candidate=name)
        print(f"P1: {name} {reports[name]['status']}", flush=True)
    try:
        selected = select_engine(reports["Toil"], reports["Weaver"], reports["gVisor"])
        status = "PASS"
        reason = "Selected by the explicit P1 gates; production integration remains out of scope"
    except ValueError as error:
        selected = None
        status = "FAIL"
        reason = str(error)
    decision = {
        "phase": "P1",
        "status": status,
        "selected_engine": selected,
        "reason": reason,
        "started_at": timestamp,
        "finished_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "docker": subprocess.check_output(
            [
                docker_executable(),
                "version",
                "--format",
                "{{.Client.Version}} / {{.Server.Version}}",
            ],
            text=True,
        ).strip(),
        "sources_sha256": source_hashes(),
        "reports": reports,
        "run_directory": str(directory.relative_to(ROOT)),
    }
    (directory / "decision.json").write_text(json.dumps(decision, indent=2))
    if args.record:
        args.record.parent.mkdir(parents=True, exist_ok=True)
        args.record.write_text(json.dumps(decision, indent=2) + "\n")
    print(
        json.dumps(
            {
                "phase": "P1",
                "status": status,
                "selected_engine": selected,
                "evidence": str(directory / "decision.json"),
            },
            indent=2,
        )
    )
    if status != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
