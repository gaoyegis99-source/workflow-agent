"""P1-only Weaver comparison client; all services and jobs are disposable probes."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PINS = json.loads((HERE / "pins.json").read_text())


class WeaverProbe:
    def __init__(self, directory: Path):
        self.directory = directory
        self.project = "v7-p1-weaver-" + uuid.uuid4().hex[:8]
        self.base = ""
        self.outputs_base = ""
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.trace = []
        self.containers: set[str] = set()

    def compose(self, *args: str, check: bool = True) -> str:
        env = os.environ.copy()
        env["V7_P1_ROOT"] = str(ROOT)
        result = subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                self.project,
                "-f",
                str(HERE / "weaver.compose.yaml"),
                *args,
            ],
            env=env,
            text=True,
            capture_output=True,
            timeout=120,
        )
        if check and result.returncode:
            raise RuntimeError(result.stderr[-3000:])
        return result.stdout + (result.stderr if result.returncode else "")

    def request(self, path: str, method: str = "GET", body: dict | None = None) -> tuple[int, dict]:
        base = self.base
        if path.startswith("http"):
            parsed = urllib.parse.urlsplit(path)
            if (parsed.hostname, parsed.port) == ("outputs.p1.test", 8000):
                if not parsed.path.startswith("/wpsoutputs/"):
                    raise ValueError("Unexpected output path")
                base = self.outputs_base
            elif (parsed.hostname, parsed.port) != ("weaver.p1.test", 4001):
                raise ValueError("Unexpected external response URL")
            path = parsed.path + ("?" + parsed.query if parsed.query else "")
        request = urllib.request.Request(
            base + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        try:
            response = self.http.open(request, timeout=15)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            raw = response.read().decode()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = {"raw": raw}
            code = response.status
        self.trace.append({"method": method, "path": path, "http_status": code, "body": data})
        return code, data

    def start(self) -> None:
        for name in ["outputs", "tmp"]:
            (ROOT / ".p1-runtime/weaver" / name).mkdir(parents=True, exist_ok=True)
        (ROOT / ".p1-runtime/weaver/weaver.ini").write_text(
            (HERE / "weaver.ini").read_text().replace("__V7_P1_ROOT__", str(ROOT))
        )
        self.compose("up", "-d")
        self.base = "http://" + self.compose("port", "manager", "4001").strip()
        self.outputs_base = "http://" + self.compose("port", "outputs", "8000").strip()
        self.ready()

    def ready(self) -> None:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                code, _ = self.request("/processes")
                if code == 200:
                    return
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                pass
            time.sleep(1)
        raise TimeoutError("Weaver API did not become ready")

    def deploy(self, identifier: str, document: dict) -> None:
        document["id"] = identifier
        code, result = self.request(
            "/processes",
            "POST",
            {
                "processDescription": {"id": identifier},
                "executionUnit": [{"unit": document}],
            },
        )
        assert code == 201, (code, result)

    def submit(self, process_id: str, inputs: dict) -> str:
        code, data = self.request(
            f"/processes/{process_id}/execution",
            "POST",
            {
                "inputs": inputs,
                "mode": "async",
                "response": "document",
                "outputs": {
                    "records" if process_id.endswith("nested") else "record": {
                        "transmissionMode": "reference"
                    }
                },
            },
        )
        assert code in {201, 202}, (code, data)
        return data["jobID"]

    def wait_job(self, job: str, states: set[str], timeout: float = 120) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            code, result = self.request("/jobs/" + job)
            assert code == 200, (code, result)
            if result["status"] in states:
                return result
            if result["status"] in {"failed", "dismissed"}:
                self.request(f"/jobs/{job}/logs")
                raise AssertionError(f"Job {job} ended as {result['status']}")
            time.sleep(0.5)
        raise TimeoutError(f"Job {job} did not reach {states}")

    def records(self, job: str) -> list[dict]:
        code, result = self.request(f"/jobs/{job}/results")
        assert code == 200, (code, result)
        references = []

        def walk(value):
            if isinstance(value, dict):
                if "href" in value and "/wpsoutputs/" in value["href"]:
                    references.append(value["href"])
                else:
                    for item in value.values():
                        walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(result)
        assert references, result
        records = []
        for reference in references:
            code, record = self.request(reference)
            assert code == 200, (code, record)
            records.append(record)
        return records

    def tool_containers(self) -> set[str]:
        ids = subprocess.check_output(
            ["docker", "ps", "-q", "--filter", "ancestor=" + PINS["python"]], text=True
        ).split()
        owned = set()
        prefix = str(ROOT / ".p1-runtime/weaver/tmp") + "/"
        for container_id in ids:
            data = json.loads(
                subprocess.check_output(["docker", "inspect", container_id], text=True)
            )[0]
            if any(mount.get("Source", "").startswith(prefix) for mount in data["Mounts"]):
                owned.add(container_id)
        self.containers |= owned
        return owned

    def cleanup(self) -> None:
        (self.directory / "service.log").write_text(self.compose("logs", "--no-color", check=False))
        self.tool_containers()
        for container_id in self.containers:
            subprocess.run(["docker", "rm", "-f", container_id], capture_output=True, timeout=20)
        self.compose("down", "-v", "--remove-orphans", check=False)
        (self.directory / "http-trace.json").write_text(json.dumps(self.trace, indent=2))


def suite(directory: Path) -> dict:
    directory.mkdir(parents=True)
    probe = WeaverProbe(directory)
    checks = []
    simple_job = None
    base_inputs = {
        "run_id": "weaver-p1",
        "step_path": "outer/scatter",
        "ordinal": 0,
        "value": 7,
        "delay": 0,
        "fail_once": "none",
        "started_file": "none",
    }

    def check(name, action):
        try:
            evidence = action()
            checks.append({"check": name, "status": "PASS", "evidence": evidence})
            return True
        except (
            AssertionError,
            TimeoutError,
            RuntimeError,
            ValueError,
            KeyError,
            urllib.error.URLError,
        ) as error:
            checks.append({"check": name, "status": "FAIL", "error": str(error)})
            return False

    try:
        probe.start()
        tool = json.loads((HERE / "fixtures/tool.cwl").read_text())
        # Same program and input contract; embed the script to avoid an unrelated
        # staging HTTP server. The sole execution-environment change is explicit.
        tool["inputs"].pop("script")
        tool["baseCommand"] = ["python3", "-c", (HERE / "fixtures/probe.py").read_text()]
        tool["requirements"]["DockerRequirement"] = {"dockerPull": PINS["python"]}
        probe.deploy("v7-p1-probe", tool)

        def simple():
            nonlocal simple_job
            job = probe.submit("v7-p1-probe", base_inputs)
            simple_job = job
            probe.wait_job(job, {"successful"})
            records = probe.records(job)
            assert len(records) == 1 and records[0]["result"] == 14
            return {"job_id": job, "records": records}

        check("submit_query_output", simple)

        def nested():
            for filename, identifier, dependency in [
                ("scatter.cwl", "v7-p1-scatter", "v7-p1-probe"),
                ("nested.cwl", "v7-p1-nested", "v7-p1-scatter"),
            ]:
                document = json.loads((HERE / "fixtures" / filename).read_text())
                document["inputs"].pop("script")
                for step in document["steps"].values():
                    step["in"].pop("script")
                    step["run"] = f"http://weaver.p1.test:4001/processes/{dependency}"
                probe.deploy(identifier, document)
            values = dict(base_inputs)
            values.pop("ordinal")
            values.pop("value")
            values.update(ordinals=[0, 1], values=[7, 7])
            job = probe.submit("v7-p1-nested", values)
            probe.wait_job(job, {"successful"})
            records = probe.records(job)
            assert sorted(r["result"] for r in records) == [14, 14]
            assert len({r["invocation_id"] for r in records}) == 2
            assert sorted(r["scatter_index"] for r in records) == [0, 1]
            assert all(r["step_path"] == "outer/scatter" for r in records)
            code, jobs = probe.request("/jobs?detail=true&process=v7-p1-probe")
            assert code == 200, (code, jobs)
            child_ids = [item["jobID"] for item in jobs["jobs"] if item["jobID"] != simple_job]
            assert len(set(child_ids)) == 2, child_ids
            definitions = []
            for child in child_ids:
                code, definition = probe.request(f"/jobs/{child}/inputs")
                assert code == 200, (code, definition)
                definitions.append({"job_id": child, "inputs": definition["inputs"]})
            assert sorted(d["inputs"]["ordinal"] for d in definitions) == [0, 1]
            assert all(
                d["inputs"]["run_id"] == "weaver-p1"
                and d["inputs"]["step_path"] == "outer/scatter"
                and d["inputs"]["value"] == 7
                for d in definitions
            )
            return {
                "job_id": job,
                "records": records,
                "child_ids": child_ids,
                "definitions": definitions,
            }

        # A result-download failure must not suppress independent restart/cancel probes.
        if simple_job:
            check("nested_duplicate_scatter_identity", nested)

            def restart():
                job = probe.submit("v7-p1-probe", dict(base_inputs, delay=20))
                probe.wait_job(job, {"running", "successful"})
                probe.compose("restart", "manager")
                probe.base = "http://" + probe.compose("port", "manager", "4001").strip()
                probe.ready()
                data = probe.wait_job(job, {"successful"})
                assert data["jobID"] == job
                return {"job_id": job, "status_after_restart": data["status"]}

            check("api_restart_relocates_job", restart)

            def cancel():
                before = probe.tool_containers()
                job = probe.submit("v7-p1-probe", dict(base_inputs, delay=60))
                deadline = time.monotonic() + 45
                while not (children := probe.tool_containers() - before):
                    assert time.monotonic() < deadline, "No live probe container observed"
                    probe.wait_job(job, {"running"}, timeout=15)
                    time.sleep(0.5)
                code, dismissed = probe.request("/jobs/" + job, "DELETE")
                assert code in {200, 202}, (code, dismissed)
                probe.wait_job(job, {"dismissed"})
                deadline = time.monotonic() + 15
                while remaining := probe.tool_containers() & children:
                    assert time.monotonic() < deadline, (
                        f"Dismissed job {job} still has running tool containers after 15s: "
                        f"{sorted(remaining)}"
                    )
                    time.sleep(0.5)
                code, _ = probe.request(f"/jobs/{job}/results")
                assert code != 200
                return {"job_id": job, "container_ids": sorted(children), "tools_stopped": True}

            check("cancel_stops_actual_tool", cancel)
        else:
            for name in [
                "nested_duplicate_scatter_identity",
                "api_restart_relocates_job",
                "cancel_stops_actual_tool",
            ]:
                checks.append(
                    {"check": name, "status": "NOT_RUN", "reason": "basic execution failed"}
                )
    finally:
        probe.cleanup()
    return {
        "candidate": "Weaver",
        "version": "6.16.0",
        "image": PINS["weaver"],
        "status": "PASS" if all(c["status"] == "PASS" for c in checks) else "FAIL",
        "checks": checks,
        "docker_cli_sha256": hashlib.sha256(Path("/usr/bin/docker").read_bytes()).hexdigest(),
    }


def main():
    if sys.flags.optimize:
        raise RuntimeError("Probe assertions must not be disabled")
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    result = suite(args.directory.resolve())
    (args.directory / "report.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))
    if result["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
