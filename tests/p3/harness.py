"""Owned processes and containers for real HTTP/container acceptance, never V6 services."""

import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

from tests.p2.processes import Processes, eventually


def _published_docker_ports():
    """Return host ports already published by running Docker containers.

    Docker's userland proxy is not guaranteed to hold a host socket open, so a
    plain ``bind(0)`` probe can select a port that Docker already owns.  This
    is particularly visible when the session-scoped P2 database is alive while
    the P3 compose bootstrap starts.
    """
    result = subprocess.run(
        ["docker", "ps", "--format", "{{.Ports}}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if result.returncode:
        return set()
    return {
        int(value)
        for value in re.findall(r"(?:127\.0\.0\.1|0\.0\.0\.0|::):([0-9]+)->", result.stdout)
    }


def free_port():
    published = _published_docker_ports()
    for _ in range(20):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        if port not in published:
            return port
    raise RuntimeError("could not allocate a host port not published by Docker")


def free_ports(count):
    """Reserve all requested ports together so the kernel cannot reuse one."""
    published = _published_docker_ports()
    sockets = []
    try:
        for _ in range(count):
            for _attempt in range(20):
                sock = socket.socket()
                sock.bind(("127.0.0.1", 0))
                if sock.getsockname()[1] not in published:
                    sockets.append(sock)
                    break
                sock.close()
            else:
                raise RuntimeError("could not allocate host ports not published by Docker")
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


def cleanup_test_containers(test_run_id):
    """Remove only containers owned by one test run, after rechecking each label."""
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{15,127}", test_run_id or "") is None:
        raise ValueError("Invalid V7 test run identity")
    result = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label=v7.test-run={test_run_id}"],
        capture_output=True,
        check=False,
        timeout=10,
    )
    if result.returncode:
        return 0
    removed = 0
    for container_id in result.stdout.decode().split():
        inspected = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                '{{ index .Config.Labels "v7.test-run" }}',
                container_id,
            ],
            capture_output=True,
            check=False,
            timeout=10,
        )
        if inspected.returncode == 0 and inspected.stdout.decode().strip() == test_run_id:
            subprocess.run(
                ["docker", "rm", "-f", container_id],
                capture_output=True,
                check=False,
                timeout=20,
            )
            removed += 1
    return removed


class Cluster(Processes):
    def __init__(self, directory, database, service_databases):
        super().__init__(directory, database.engine.url.render_as_string(hide_password=False))
        runtime_root = Path(os.environ.get("V7_P3_RUNTIME_ROOT", ".p3-runtime"))
        images = json.loads((runtime_root / "images.json").read_text())
        data, execution = service_databases
        self.database, self.service_databases = database, service_databases
        self.test_run_id = secrets.token_hex(16)
        names = ["data", "execution", "platform"]
        self.ports = dict(zip(names, free_ports(len(names)), strict=True))
        self.env = {
            "V7_DATABASE_URL": self.database_url,
            "V7_DATA_DATABASE_URL": data.engine.url.render_as_string(hide_password=False),
            "V7_EXECUTION_DATABASE_URL": execution.engine.url.render_as_string(hide_password=False),
            "V7_DATA_STORAGE": str(directory / "assets"),
            "V7_EXECUTION_WORKDIR": str(directory / "work"),
            "V7_OPERATOR_IMAGE": images["operator"],
            "V7_CHECKER_IMAGE": images["checker"],
            "V7_TEST_RUN_ID": self.test_run_id,
        }
        for name in ["data", "execution", "platform"]:
            self.env[f"V7_{name.upper()}_URL"] = f"http://127.0.0.1:{self.ports[name]}"
            self.env[f"V7_{name.upper()}_PORT"] = str(self.ports[name])
        for name in ["PLATFORM_DATA", "EXECUTION_DATA", "PLATFORM_EXECUTION", "EXECUTION_PLATFORM"]:
            self.env[f"V7_{name}_TOKEN"] = secrets.token_urlsafe(32)
        self.running = {}
        self.client = httpx.Client(base_url=self.env["V7_PLATFORM_URL"], trust_env=False, timeout=8)

    def launch(self, name, command=None):
        modules = {
            "data": "platform_data.api",
            "execution": "platform_execution.api",
            "data-worker": "platform_data.worker",
            "data-spatial-worker": "platform_data.spatial_worker",
            "execution-worker": "platform_execution.worker",
            "platform-worker": "platform_infrastructure.worker",
        }
        if command is not None:
            args = command
        elif name == "platform":
            args = [sys.executable, "-m", "platform_api", "--port", self.ports[name]]
        else:
            args = [sys.executable, "-c", f"from {modules[name]} import main; main()"]
        process = self.start(args, self.env)
        self.running[name] = process
        if name in self.ports:
            path = "/api/v1/ready" if name == "platform" else "/readyz"

            def ready():
                if process.poll() is not None:
                    raise AssertionError(f"{name} exited; inspect {self.directory}")
                try:
                    return (
                        httpx.get(
                            self.env[f"V7_{name.upper()}_URL"] + path, timeout=1, trust_env=False
                        ).status_code
                        == 200
                    )
                except httpx.TransportError:
                    return False

            eventually(ready, timeout=20)
        return process

    def restart(self, name):
        self.kill(self.running[name])
        return self.launch(name)

    def start_services(self, execution_command=None):
        for name in ["data", "execution", "platform", "data-worker"]:
            self.launch(name, execution_command if name == "execution" else None)

    def request(self, method, path, body=None, key=None):
        headers = {"Idempotency-Key": key or secrets.token_hex(12)}
        response = self.client.request(method, path, json=body, headers=headers)
        assert response.is_success, (response.status_code, response.text)
        return response.json()

    def put_content(self, path, content, timeout=30):
        """Repeat the same immutable upload after a transient or lost response."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                response = self.client.put(path, content=content)
            except httpx.TimeoutException:
                if time.monotonic() >= deadline:
                    raise
                continue
            if response.status_code != 503:
                assert response.status_code == 200, (response.status_code, response.text)
                return response.json()
            error = response.json()
            assert error["code"] == "DEPENDENCY_UNAVAILABLE" and error["retry_mode"] == "SAME_KEY"
            if time.monotonic() >= deadline:
                raise AssertionError((response.status_code, response.text))
            time.sleep(0.1)

    def bootstrap(
        self,
        token,
        *,
        budget=3,
        max_in_flight=2,
        raw=None,
        parameters=None,
        project_name="Synthetic P3",
    ):
        self.client.headers["Authorization"] = "Bearer " + token
        started = time.perf_counter()
        project = self.request("POST", "/api/v1/projects", {"name": project_name})
        self.project = project["project_id"]
        self.p = "/api/v1/projects/" + self.project
        raw = raw or b"station,time,value\nA,2024-01-01,1\nA,2024-01-02,3\nA,2024-01-03,5\n"
        import hashlib

        upload = self.request(
            "POST",
            self.p + "/uploads",
            {
                "filename": "synthetic.csv",
                "format": "csv",
                "size": len(raw),
                "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
            },
        )
        path = self.p + "/uploads/" + upload["upload_id"]
        self.put_content(path + "/content", raw)
        self.request(
            "POST",
            path + "/finalize",
            {
                "columns": [
                    {"name": name, "type": "string"} for name in ["station", "time", "value"]
                ]
            },
        )

        def imported():
            value = self.request("GET", path)
            assert value["status"] != "FAILED", value
            return value if value["status"] == "READY" else None

        upload = eventually(imported, timeout=20)
        ref = upload["asset"]
        self.request(
            "POST",
            self.p + "/assets/" + ref["asset_id"] + "/semantic-revisions",
            {
                "expected_revision": 0,
                "version_id": "1",
                "semantics": {
                    "timezone": "UTC",
                    "value_unit": "m",
                    "quantity": "water_level_elevation",
                },
            },
        )
        asset = self.request("GET", self.p + "/assets/" + ref["asset_id"] + "/versions/1")
        self.selection = {
            "asset": ref,
            "representation_id": "normalized",
            "representation_digest": asset["manifest"]["representations"][1]["digest"],
            "semantic_revision": 1,
            "field_bindings": {"groupField": "f0", "timeField": "f1", "valueField": "f2"},
        }
        self.parameters = parameters or {
            "groups": ["A", "absent"],
            "start": "2024-01-01T00:00:00+00:00",
            "end": "2024-01-05T00:00:00+00:00",
            "frequency": "day",
            "min_samples": 3,
        }
        self.spec = {
            "profile": "p3-timeseries-v1",
            "input": self.selection,
            "parameters": self.parameters,
            "required_outputs": ["timeseries-completeness", "statistics-linear-trend"],
            "allow_not_evaluable": True,
            "minimum_evaluable": 1,
        }
        task = self.request(
            "POST",
            self.p + "/tasks",
            {
                "execution_mode": "DIRECT",
                "spec": {
                    "goal": "Synthetic completeness and descriptive trend",
                    "requirements": [],
                    "acceptance_spec": self.spec,
                },
                "budget": {"execution_count": budget, "max_in_flight": max_in_flight},
            },
        )
        self.task = task["task_id"]
        from datetime import UTC, datetime, timedelta

        from platform_core.application.catalog import OPERATORS, target

        self.targets = [target(value) for value in OPERATORS]
        self.grant = self.request(
            "POST",
            self.p + "/grants",
            {
                "task_id": self.task,
                "targets": self.targets,
                "resources": [ref],
                "derived_outputs": True,
                "acceptance_profiles": ["p3-timeseries-v1"],
                "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            },
        )
        self.bootstrap_seconds = time.perf_counter() - started

    def submit(self, index=0, key=None):
        return self.request(
            "POST",
            self.p + "/executions",
            {
                "task_id": self.task,
                "expected_spec_revision": 1,
                "target": self.targets[index],
                "inputs": {"observations": self.selection},
                "parameters": self.parameters,
            },
            key,
        )

    def completed_execution(self, command, timeout=60):
        def done():
            receipt = self.request("GET", command["status_url"])
            if not receipt["result"]:
                assert receipt["status"] != "REJECTED", receipt
                return None
            execution = self.request(
                "GET", self.p + "/executions/" + receipt["result"]["execution_id"]
            )
            if execution["state"] in ["SUCCEEDED", "FAILED", "CANCELLED", "REJECTED"]:
                return execution
            return None

        return eventually(done, timeout=timeout)

    def close(self):
        # Cleanup must run even if a child process or log close raises.  Workers
        # can create operator containers while they are being stopped, so the
        # label cleanup happens after process termination and is retried.
        try:
            super().close()
        finally:
            try:
                self.client.close()
            finally:
                for attempt in range(3):
                    if cleanup_test_containers(self.test_run_id) == 0:
                        break
                    if attempt < 2:
                        time.sleep(0.2)
                # Only containers whose stable identities belong to this test's
                # private workdir.
                for path in (self.directory / "work").glob("e*"):
                    subprocess.run(
                        ["docker", "rm", "-f", "v7-p3-" + path.name],
                        capture_output=True,
                        timeout=10,
                    )
