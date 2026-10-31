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

from tests.platform.database.processes import Processes, eventually


def docker_executable() -> str:
    """Resolve the guarded project launcher in-source or for a copied driver."""

    configured = os.environ.get("V7_TEST_DOCKER_EXECUTABLE")
    if configured:
        candidate = Path(configured)
        try:
            valid = (
                candidate.is_absolute()
                and candidate.name == "docker"
                and not candidate.is_symlink()
                and candidate.is_file()
                and candidate.stat().st_uid == os.getuid()
                and os.access(candidate, os.X_OK)
            )
        except OSError:
            valid = False
        if not valid:
            raise RuntimeError("V7_TEST_DOCKER_EXECUTABLE_INVALID")
        return str(candidate)

    maintenance_source = next(
        (
            parent
            for parent in Path(__file__).resolve().parents
            if (parent / "scripts/maintenance/project_environment.py").is_file()
        ),
        None,
    )
    if maintenance_source is None:
        raise RuntimeError("V7_TEST_DOCKER_EXECUTABLE_REQUIRED")
    if str(maintenance_source) not in sys.path:
        sys.path.insert(0, str(maintenance_source))
    from scripts.maintenance.project_environment import docker_executable as project_docker

    return project_docker()


def _project_root() -> Path:
    """Resolve a source checkout or an extracted distribution test root."""
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "project-layout.json").is_file():
            return candidate
    return Path(__file__).resolve().parents[3]


P3_RUNTIME_ROOT = (
    Path(os.environ.get("V7_P3_RUNTIME_ROOT", _project_root() / "runtime/work/p3"))
    .expanduser()
    .resolve()
)


def _published_docker_ports():
    """Return host ports already published by running Docker containers.

    Docker's userland proxy is not guaranteed to hold a host socket open, so a
    plain ``bind(0)`` probe can select a port that Docker already owns.  This
    is particularly visible when the session-scoped P2 database is alive while
    the P3 compose bootstrap starts.
    """
    result = subprocess.run(
        [docker_executable(), "ps", "--format", "{{.Ports}}"],
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


def container_callback_host():
    """Return the host address reachable from the isolated rootless daemon.

    The project daemon deliberately starts slirp4netns with host-loopback
    disabled.  A workflow container therefore cannot call a test API through
    127.0.0.1 or the rootless bridge gateway.  The address selected by the
    host's default IPv4 route remains reachable without granting the container
    host networking.
    """

    override = os.environ.get("V7_TEST_CONTAINER_CALLBACK_HOST")
    if override:
        try:
            socket.inet_aton(override)
        except OSError as error:
            raise ValueError("V7_TEST_CONTAINER_CALLBACK_HOST must be an IPv4 address") from error
        if override.startswith("127.") or override == "0.0.0.0":
            raise ValueError("V7_TEST_CONTAINER_CALLBACK_HOST must be container-reachable")
        return override
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
        # UDP connect performs route selection without sending a packet.
        route.connect(("192.0.2.1", 9))
        value = route.getsockname()[0]
    if value.startswith("127.") or value == "0.0.0.0":
        raise RuntimeError("No container-reachable test callback address is available")
    return value


def cleanup_test_containers(test_run_id):
    """Remove only containers owned by one test run, after rechecking each label.

    The return value includes both removals and unresolved candidates so callers
    can retry when a bounded Docker cleanup command times out.
    """
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{15,127}", test_run_id or "") is None:
        raise ValueError("Invalid V7 test run identity")
    try:
        result = subprocess.run(
            [
                docker_executable(),
                "ps",
                "-aq",
                "--filter",
                f"label=v7.test-run={test_run_id}",
            ],
            capture_output=True,
            check=False,
            timeout=10,
        )
    except subprocess.TimeoutExpired:
        return 1
    if result.returncode:
        return 0
    removed = 0
    unresolved = 0
    for container_id in result.stdout.decode().split():
        try:
            inspected = subprocess.run(
                [
                    docker_executable(),
                    "inspect",
                    "--format",
                    '{{ index .Config.Labels "v7.test-run" }}',
                    container_id,
                ],
                capture_output=True,
                check=False,
                timeout=10,
            )
        except subprocess.TimeoutExpired:
            unresolved += 1
            continue
        if inspected.returncode == 0 and inspected.stdout.decode().strip() == test_run_id:
            try:
                removal = subprocess.run(
                    [docker_executable(), "rm", "-f", container_id],
                    capture_output=True,
                    check=False,
                    timeout=20,
                )
            except subprocess.TimeoutExpired:
                unresolved += 1
                continue
            if removal.returncode == 0:
                removed += 1
            else:
                unresolved += 1
    return removed + unresolved


def kill_owned_test_container(container_name, test_run_id, timeout=30):
    """Kill one proven test container, tolerating a lost Docker CLI response."""

    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{15,127}", test_run_id or "") is None:
        raise ValueError("Invalid V7 test run identity")
    if re.fullmatch(r"v7-p[34]-[a-z0-9][a-z0-9_.-]{15,127}", container_name or "") is None:
        raise ValueError("Invalid V7 test container name")
    if timeout <= 0:
        raise ValueError("Container kill timeout must be positive")

    launcher = docker_executable()
    deadline = time.monotonic() + timeout

    def inspect_state():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "timeout"
        try:
            inspected = subprocess.run(
                [
                    launcher,
                    "inspect",
                    "--format",
                    '{{.State.Running}}\t{{ index .Config.Labels "v7.test-run" }}',
                    container_name,
                ],
                capture_output=True,
                check=False,
                timeout=min(10, max(0.1, remaining)),
            )
        except subprocess.TimeoutExpired:
            return "timeout"
        if inspected.returncode:
            return "missing"
        running, separator, owner = inspected.stdout.decode().strip().partition("\t")
        if not separator or owner != test_run_id:
            raise RuntimeError("Refusing to kill an unowned V7 test container")
        if running not in {"true", "false"}:
            raise RuntimeError("Docker returned an invalid container state")
        return "running" if running == "true" else "stopped"

    while (initial := inspect_state()) == "timeout" and time.monotonic() < deadline:
        time.sleep(0.1)
    if initial != "running":
        raise RuntimeError(f"V7 test container is not running: {container_name}")

    last_error = "Docker did not confirm the kill request"
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            killed = subprocess.run(
                [launcher, "kill", container_name],
                capture_output=True,
                check=False,
                timeout=min(10, max(0.1, remaining)),
            )
            if killed.returncode:
                last_error = killed.stderr.decode(errors="replace").strip() or last_error
        except subprocess.TimeoutExpired:
            # The daemon may have accepted SIGKILL even when the CLI response
            # was lost. Re-inspect the proven target before issuing a retry.
            last_error = "Docker kill response timed out"
        state = inspect_state()
        if state in {"stopped", "missing"}:
            return
        if time.monotonic() < deadline:
            time.sleep(0.1)
    raise TimeoutError(f"Could not kill V7 test container {container_name}: {last_error}")


class ProcessGroup:
    """A test-only handle for one explicitly configured process per worker lane."""

    def __init__(self, processes):
        self.processes = tuple(processes)

    def poll(self):
        statuses = [process.poll() for process in self.processes]
        return next((status for status in statuses if status is not None), None)

    def kill(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()

    def terminate(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()

    def send_signal(self, signum):
        for process in self.processes:
            if process.poll() is None:
                process.send_signal(signum)

    def wait(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        statuses = []
        for process in self.processes:
            remaining = None if deadline is None else max(0, deadline - time.monotonic())
            statuses.append(process.wait(timeout=remaining))
        return statuses[0]


class Cluster(Processes):
    PLATFORM_WORKER_LANES = ("package", "action", "acceptance", "knowledge")
    DEFAULT_PLATFORM_WORKER_LANES = ("package", "action", "acceptance")

    def __init__(self, directory, database, service_databases):
        super().__init__(directory, database.engine.url.render_as_string(hide_password=False))
        runtime_root = P3_RUNTIME_ROOT
        images = json.loads((runtime_root / "images.json").read_text())
        data, execution = service_databases
        self.database, self.service_databases = database, service_databases
        self.test_run_id = secrets.token_hex(16)
        self.workload_network = f"v7-test-{self.test_run_id}-workload"
        names = ["data", "execution", "platform"]
        self.ports = dict(zip(names, free_ports(len(names)), strict=True))
        self.env = self.platform_environment | {
            "V7_DATABASE_URL": self.database_url,
            "V7_DATA_DATABASE_URL": data.engine.url.render_as_string(hide_password=False),
            "V7_EXECUTION_DATABASE_URL": execution.engine.url.render_as_string(hide_password=False),
            "V7_DATA_STORAGE": str(directory / "assets"),
            "V7_EXECUTION_WORKDIR": str(directory / "work"),
            "V7_OPERATOR_IMAGE": images["operator"],
            "V7_CHECKER_IMAGE": images["checker"],
            "V7_TEST_RUN_ID": self.test_run_id,
        }
        from platform_core.application.catalog import load_catalog
        from platform_core.application.checker_catalog import (
            load_catalog as load_checker_catalog,
        )

        self.operator_catalog = load_catalog(self.env["V7_OPERATOR_CATALOG"])
        self.checker_catalog = load_checker_catalog(
            self.env["V7_CHECKER_CATALOG"],
            {
                "CHECKER": self.env["V7_CHECKER_IMAGE"],
                "GDAL_CHECKER": self.env["V7_GDAL_CHECKER_IMAGE"],
            },
        )
        for name in ["data", "execution", "platform"]:
            self.env[f"V7_{name.upper()}_URL"] = f"http://127.0.0.1:{self.ports[name]}"
            self.env[f"V7_{name.upper()}_PORT"] = str(self.ports[name])
        # The workflow engine calls the same private Execution API that this
        # harness has just allocated.  Rootless host-loopback is disabled, so
        # expose only this token-protected test API on the host route selected
        # above and give every harness its own Docker workload network.
        self.container_callback_host = container_callback_host()
        self.workflow_callback_url = (
            f"http://{self.container_callback_host}:{self.ports['execution']}"
        )
        self.env |= {
            "V7_EXECUTION_CALLBACK_URL": self.workflow_callback_url,
            "V7_EXECUTION_HOST": "0.0.0.0",
            "V7_EXECUTION_WORKLOAD_NETWORK": self.workload_network,
        }
        for name in [
            "PLATFORM_DATA",
            "EXECUTION_DATA",
            "KNOWLEDGE_DATA",
            "PLATFORM_EXECUTION",
            "EXECUTION_PLATFORM",
        ]:
            self.env[f"V7_{name}_TOKEN"] = secrets.token_urlsafe(32)
        self.running = {}
        self.workload_network_created = False
        self.client = httpx.Client(base_url=self.env["V7_PLATFORM_URL"], trust_env=False, timeout=8)

    def configure_checker_catalog(self, path):
        """Install one checker lock in both process and in-memory test composition."""

        from platform_core.application.checker_catalog import (
            load_catalog as load_checker_catalog,
        )

        catalog_path = Path(path).expanduser().resolve()
        try:
            lock = json.loads(catalog_path.read_text(encoding="utf-8"))
            images = lock["images"]
            checker_image = images["CHECKER"]
            gdal_checker_image = images["GDAL_CHECKER"]
        except (OSError, KeyError, TypeError, ValueError) as error:
            raise ValueError("Checker catalogue lock cannot be configured") from error
        self.env |= {
            "V7_CHECKER_CATALOG": str(catalog_path),
            "V7_CHECKER_IMAGE": checker_image,
            "V7_GDAL_CHECKER_IMAGE": gdal_checker_image,
        }
        self.checker_catalog = load_checker_catalog(
            catalog_path,
            {"CHECKER": checker_image, "GDAL_CHECKER": gdal_checker_image},
        )
        return self.checker_catalog

    def ensure_workload_network(self):
        """Create the private workflow network owned by this test harness."""

        if self.workload_network_created:
            return
        result = subprocess.run(
            [
                docker_executable(),
                "network",
                "create",
                "--label",
                f"v7.test-run={self.test_run_id}",
                self.workload_network,
            ],
            capture_output=True,
            check=False,
            timeout=15,
        )
        if result.returncode:
            raise RuntimeError(
                "Could not create the isolated workflow test network: "
                + result.stderr.decode(errors="replace").strip()
            )
        self.workload_network_created = True

    def cleanup_workload_network(self):
        """Remove only the exact network whose ownership label we can prove."""

        if not self.workload_network_created:
            return
        for attempt in range(10):
            try:
                inspected = subprocess.run(
                    [
                        docker_executable(),
                        "network",
                        "inspect",
                        "--format",
                        '{{ index .Labels "v7.test-run" }}',
                        self.workload_network,
                    ],
                    capture_output=True,
                    check=False,
                    timeout=10,
                )
            except subprocess.TimeoutExpired:
                inspected = None
            if inspected is not None:
                if inspected.returncode:
                    self.workload_network_created = False
                    return
                if inspected.stdout.decode().strip() != self.test_run_id:
                    raise RuntimeError("Refusing to remove an unowned workflow test network")
                try:
                    removal = subprocess.run(
                        [docker_executable(), "network", "rm", self.workload_network],
                        capture_output=True,
                        check=False,
                        timeout=15,
                    )
                except subprocess.TimeoutExpired:
                    removal = None
                if removal is not None and removal.returncode == 0:
                    self.workload_network_created = False
                    return
            if attempt < 9:
                time.sleep(min(0.2 * (2**attempt), 1.0))
        raise RuntimeError("Could not remove the owned workflow test network")

    def launch(self, name, command=None):
        modules = {
            "data": "platform_data.api",
            "execution": "platform_execution.api",
            "data-worker": "platform_data.worker",
            "data-spatial-worker": "platform_data.spatial_worker",
            "execution-worker": "platform_execution.worker",
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

    def launch_platform_worker_lanes(self, lanes=DEFAULT_PLATFORM_WORKER_LANES):
        lanes = tuple(lanes)
        if not lanes or len(lanes) != len(set(lanes)):
            raise ValueError("Platform worker lanes must be non-empty and unique")
        unknown = set(lanes) - set(self.PLATFORM_WORKER_LANES)
        if unknown:
            raise ValueError(f"Unknown Platform worker lanes: {sorted(unknown)}")
        processes = [
            self.start(
                [
                    sys.executable,
                    "-c",
                    "from platform_infrastructure.worker import main; main()",
                    "--lane",
                    lane,
                ],
                self.env,
            )
            for lane in lanes
        ]
        group = ProcessGroup(processes)
        self.running["platform-worker-lanes"] = group
        return group

    def restart_platform_worker_lanes(self, lanes=DEFAULT_PLATFORM_WORKER_LANES):
        self.kill(self.running["platform-worker-lanes"])
        return self.launch_platform_worker_lanes(lanes)

    def restart(self, name):
        self.kill(self.running[name])
        return self.launch(name)

    def install_operator_image(self, image_id):
        """Install one test image through the same explicit catalogue as production."""

        from platform_core.application.catalog import write_catalog

        self.env["V7_OPERATOR_IMAGE"] = image_id
        self.operator_catalog = write_catalog(
            self.env["V7_OPERATOR_CATALOG"],
            {
                "OPERATOR": image_id,
                "QGIS": self.env["V7_SPATIAL_QGIS_IMAGE_ID"],
                "SPATIAL_NATIVE": self.env["V7_SPATIAL_NATIVE_IMAGE_ID"],
                "TABULAR": self.env["V7_TABULAR_OPERATOR_IMAGE"],
            },
        )
        # Both APIs freeze the catalogue at startup. Restarting these private
        # test processes proves that the image is installed coherently.
        self.restart("execution")
        self.restart("platform")

    def start_services(self, execution_command=None):
        self.ensure_workload_network()
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
            try:
                receipt = self.request("GET", command["status_url"])
                if not receipt["result"]:
                    assert receipt["status"] != "REJECTED", receipt
                    return None
                execution = self.request(
                    "GET", self.p + "/executions/" + receipt["result"]["execution_id"]
                )
            except httpx.TimeoutException:
                # An individual HTTP request has a short timeout, while this
                # method owns the caller's longer eventual-completion budget.
                # Keep transient reads inside that bounded outer deadline.
                platform = self.running.get("platform")
                if platform is not None:
                    assert platform.poll() is None, f"platform exited; inspect {self.directory}"
                return None
            if execution["state"] in ["SUCCEEDED", "FAILED", "CANCELLED", "REJECTED"]:
                return execution
            return None

        return eventually(done, timeout=timeout)

    def exact_operator_role_binding(self, execution, role_id=None, output_port="result"):
        """Build the caller view of an exact binding from published HTTP facts."""

        receipt = execution["runtime"]["publication_receipt"]
        ref = receipt["asset"]
        asset = self.request(
            "GET", self.p + f"/assets/{ref['asset_id']}/versions/{ref['version_id']}"
        )
        representation = next(
            value
            for value in asset["manifest"]["representations"]
            if value["representation_id"] == output_port
        )
        operator_id = execution["request"]["target"]["operator_id"]
        return {
            "role_id": role_id or operator_id,
            "output_ref": {
                "kind": "OPERATOR_OUTPUT",
                "action_id": execution["action_id"],
                "execution_id": execution["execution_id"],
                "output_port": output_port,
                "asset_ref": ref,
                "representation_id": output_port,
                "representation_digest": representation["digest"],
                "schema_id": representation["schema_id"],
                "schema_source": "MANIFEST",
            },
        }

    def exact_workflow_role_bindings(self, execution, published_ports):
        """Build exact workflow bindings using only public immutable facts."""

        steps = self.request("GET", self.p + f"/executions/{execution['execution_id']}/steps")[
            "items"
        ]
        by_node = {step["node_id"]: step for step in steps}
        outputs = execution["runtime"]["publication_receipt"]["outputs"]
        bindings = []
        for published_port in published_ports:
            published = outputs[published_port]
            ref = published["asset"]
            asset = self.request(
                "GET", self.p + f"/assets/{ref['asset_id']}/versions/{ref['version_id']}"
            )
            representation_id = published["representation_id"]
            representation = next(
                value
                for value in asset["manifest"]["representations"]
                if value["representation_id"] == representation_id
            )
            node = by_node[asset["manifest"]["workflow"]["member_id"]]
            bindings.append(
                {
                    "role_id": asset["manifest"]["source"]["target"]["operator_id"],
                    "output_ref": {
                        "kind": "WORKFLOW_OUTPUT",
                        "parent_action_id": execution["action_id"],
                        "parent_execution_id": execution["execution_id"],
                        "node_action_id": node["node_id"],
                        "node_execution_id": node["execution_id"],
                        "call_path": node["call_path"],
                        "scatter_path": node["scatter_indices"],
                        "published_output_port": published_port,
                        "published_path": [],
                        "output_port": representation_id,
                        "asset_ref": ref,
                        "representation_id": representation_id,
                        "representation_digest": representation["digest"],
                        "schema_id": representation["schema_id"],
                        "schema_source": "MANIFEST",
                    },
                }
            )
        return sorted(bindings, key=lambda value: value["role_id"])

    def register_operator_deliverable(self, executions, *, key=None):
        bindings = [self.exact_operator_role_binding(value) for value in executions]
        bindings.sort(key=lambda value: value["role_id"])
        return self.request(
            "POST",
            self.p + f"/tasks/{self.task}/deliverables",
            {"expected_spec_revision": 1, "role_bindings": bindings},
            key,
        )

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
                    for attempt in range(3):
                        try:
                            removal = subprocess.run(
                                [docker_executable(), "rm", "-f", "v7-p3-" + path.name],
                                capture_output=True,
                                check=False,
                                timeout=10,
                            )
                        except subprocess.TimeoutExpired:
                            removal = None
                        if removal is not None and removal.returncode == 0:
                            break
                        if attempt < 2:
                            time.sleep(0.2)
                self.cleanup_workload_network()
