"""Build/install wheels outside the source tree and smoke-test a real HTTP process.

Run using `uv run --locked python scripts/checks/verify_distribution.py` after `uv sync --locked`.
Temporary environments, server and a dedicated PostgreSQL are always cleaned up.
"""

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from scripts.configure.p2_environment import PostgreSQL  # noqa: E402
from scripts.lib.runtime_layout import (  # noqa: E402
    P3_RUNTIME_ROOT,
    P4_RUNTIME_ROOT,
    P5_RUNTIME_ROOT,
    P7_RUNTIME_ROOT,
)

_SMOKE_SERVICE_TOKEN = "distribution-smoke-service-token-" + "x" * 32
_OPENAPI_SMOKE_TIMEOUT_SECONDS = 15


def _json_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError) as error:
        raise RuntimeError(f"Required distribution lock cannot be read: {path}") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"Required distribution lock is not an object: {path}")
    return value


def _image_identity(value: object, *, source: str) -> str:
    if not (
        isinstance(value, str)
        and value.startswith("sha256:")
        and len(value) == 71
        and all(character in "0123456789abcdef" for character in value[7:])
    ):
        raise RuntimeError(f"Required immutable image identity is invalid: {source}")
    return value


def _distribution_catalog_environment(directory: Path) -> dict[str, str]:
    """Materialize the frozen runtime catalogs consumed by installed processes."""

    from platform_core.application.catalog import write_catalog as write_operator_catalog
    from platform_core.application.checker_catalog import build_catalog as build_checker_catalog

    p3_images = _json_object(P3_RUNTIME_ROOT / "images.json")
    spatial_lock = _json_object(P4_RUNTIME_ROOT / "spatial/images.json")
    spatial_images = spatial_lock.get("images")
    if not isinstance(spatial_images, dict):
        raise RuntimeError("Required spatial image lock has no images object")
    checker_lock = _json_object(P5_RUNTIME_ROOT / "checker-catalog.json")
    checker_images = checker_lock.get("images")
    if not isinstance(checker_images, dict):
        raise RuntimeError("Required checker catalog has no images object")

    operator = _image_identity(p3_images.get("operator"), source="P3 operator")
    checker = _image_identity(checker_images.get("CHECKER"), source="P5 checker")
    p3_checker = _image_identity(p3_images.get("checker"), source="P3 checker")
    if checker != p3_checker:
        raise RuntimeError("P3 and P5 checker image identities differ")
    gdal_checker = _image_identity(checker_images.get("GDAL_CHECKER"), source="P5 GDAL checker")
    qgis = _image_identity(spatial_images.get("qgis"), source="P4 QGIS")
    spatial_native = _image_identity(spatial_images.get("native"), source="P4 spatial native")
    try:
        tabular_value = (P7_RUNTIME_ROOT / "tabular/image-id.txt").read_text(encoding="utf-8")
        cwl_value = (P4_RUNTIME_ROOT / "cwl-image.txt").read_text(encoding="utf-8")
    except OSError as error:
        raise RuntimeError("Required distribution image lock cannot be read") from error
    tabular = _image_identity(tabular_value.strip(), source="P7 tabular")
    cwl = _image_identity(cwl_value.strip(), source="P4 CWL")

    operator_path = directory / "operator-catalog.json"
    checker_path = directory / "checker-catalog.json"
    write_operator_catalog(
        operator_path,
        {
            "OPERATOR": operator,
            "QGIS": qgis,
            "SPATIAL_NATIVE": spatial_native,
            "TABULAR": tabular,
        },
    )
    checker_roles = {"CHECKER": checker, "GDAL_CHECKER": gdal_checker}
    checker_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "images": checker_roles,
                "catalog": build_checker_catalog(checker_roles),
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "V7_OPERATOR_CATALOG": str(operator_path),
        "V7_CHECKER_CATALOG": str(checker_path),
        "V7_OPERATOR_IMAGE": operator,
        "V7_CHECKER_IMAGE": checker,
        "V7_GDAL_CHECKER_IMAGE": gdal_checker,
        "V7_SPATIAL_QGIS_IMAGE_ID": qgis,
        "V7_SPATIAL_NATIVE_IMAGE_ID": spatial_native,
        "V7_TABULAR_OPERATOR_IMAGE": tabular,
        "V7_CWL_IMAGE": cwl,
        "V7_EXECUTION_PLATFORM_TOKEN": "distribution-smoke-internal-token-" + "x" * 32,
    }


def _idle_worker_environment(environment: dict[str, str], *, knowledge_url: str) -> dict[str, str]:
    """Configure every worker lane while keeping the smoke queue side-effect free."""

    return environment | {
        # No item is queued for these remote lanes.  A regression that causes
        # an unexpected call fails immediately against the closed loopback port.
        "V7_EXECUTION_URL": "http://127.0.0.1:1",
        "V7_PLATFORM_EXECUTION_TOKEN": _SMOKE_SERVICE_TOKEN,
        "V7_KNOWLEDGE_URL": knowledge_url,
        "V7_PLATFORM_KNOWLEDGE_TOKEN": _SMOKE_SERVICE_TOKEN,
    }


@contextmanager
def _idle_knowledge_service() -> Iterator[str]:
    """Serve exactly the empty lifecycle claim needed by a one-shot worker."""

    claims: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
            length = int(self.headers.get("Content-Length", "0"))
            try:
                payload = json.loads(self.rfile.read(length))
            except (TypeError, ValueError):
                payload = None
            if (
                self.path != "/internal/lifecycle-events/claim"
                or self.headers.get("Authorization") != "Bearer " + _SMOKE_SERVICE_TOKEN
                or not isinstance(payload, dict)
            ):
                self.send_response(400)
                self.end_headers()
                return
            claims.append(payload)
            body = b'{"event":null}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert len(claims) == 1


def run(
    args: list[str | Path],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int = 180,
) -> str:
    result = subprocess.run(
        [str(arg) for arg in args],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError(f"Command failed: {args}\n{result.stdout}\n{result.stderr}")
    return result.stdout


def smoke_worker_lanes(executable: Path, *, cwd: Path, env: dict[str, str]) -> None:
    with _idle_knowledge_service() as knowledge_url:
        configured = _idle_worker_environment(env, knowledge_url=knowledge_url)
        for lane in ("package", "action", "acceptance", "knowledge"):
            run([executable, "--lane", lane, "--once"], cwd=cwd, env=configured)


def smoke_server(
    executable: Path,
    *,
    cwd: Path,
    env: dict[str, str],
    token: str | None = None,
    legacy: dict | None = None,
) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    # Ignore machine proxy settings for this loopback-only smoke check.
    http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    origin = f"http://127.0.0.1:{port}"
    with (cwd / "server.log").open("w+") as log:
        process = subprocess.Popen(
            [str(executable), "--host", "127.0.0.1", "--port", str(port)],
            cwd=cwd,
            env=env,
            stdout=log,
            stderr=log,
        )
        try:
            deadline = time.monotonic() + 15
            while True:
                if process.poll() is not None:
                    raise RuntimeError("API exited during startup")
                try:
                    with http.open(origin + "/api/v1/health", timeout=1) as response:
                        assert response.status == 200
                        assert response.headers["X-Request-ID"]
                        assert json.load(response) == {
                            "status": "ok",
                            "service": "platform-api",
                            "phase": "P8.2",
                            "contract_version": "1",
                        }
                    break
                except urllib.error.URLError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("API did not become live within 15 seconds") from None
                    time.sleep(0.1)
            # The first schema render walks the complete route/model graph.  It
            # can legitimately exceed the short request timeout after the full
            # integration suite has exercised the host, but it must still be
            # bounded so a dead server cannot stall the release gate.
            with http.open(
                origin + "/api/v1/openapi.json", timeout=_OPENAPI_SMOKE_TIMEOUT_SECONDS
            ) as response:
                schema = json.load(response)
                assert schema["info"]["description"].startswith("P8 / A7:")
                paths = schema["paths"]
                assert "/api/v1/projects" in paths
                assert "/api/v1/projects/{project_id}/workspace-summary" in paths
                assert "/api/v1/projects/{project_id}/executions" in paths
                assert "/api/v1/projects/{project_id}/knowledge/search" in paths
                assert "/api/v1/projects/{project_id}/tasks/{task_id}/research" in paths
                assert (
                    "/api/v1/projects/{project_id}/tasks/{task_id}/acceptance/"
                    "{acceptance_id}/reviews"
                ) in paths
                assert "/api/v1/projects/{project_id}/tasks/{task_id}/agent" in paths
                assert "/api/v1/projects/{project_id}/authoring/operator-descriptors" in paths
                assert "/api/v1/projects/{project_id}/authoring/imports" in paths
                assert "/api/v1/projects/{project_id}/authoring/previews" in paths
                assert (
                    "/api/v1/projects/{project_id}/workflow-definitions/{definition_id}/validations"
                ) in paths
                assert (
                    "/api/v1/projects/{project_id}/authoring/validations/{validation_id}"
                ) in paths
                assert (
                    "/api/v1/projects/{project_id}/workflow-definitions/{definition_id}/candidates"
                    in paths
                )
                assert (
                    "/api/v1/projects/{project_id}/workflow-definitions/{definition_id}/trial-runs"
                    in paths
                )
                assert (
                    "/api/v1/projects/{project_id}/workflow-definitions/{definition_id}/releases"
                    in paths
                )
                assert "/api/v1/projects/{project_id}/workflow-releases" in paths
                assert (
                    "/api/v1/projects/{project_id}/workflow-definitions/{definition_id}/"
                    "releases/{version_label}/executions"
                ) in paths
                assert (
                    "/api/v1/projects/{project_id}/workflow-definitions/{definition_id}/"
                    "releases/{version_label}/lifecycle-revisions"
                ) in paths
                assert "/api/v1/projects/{project_id}/workflow-definitions" in paths
                assert "/api/v1/projects/{project_id}/operator-proposals" in paths
                assert not any(
                    "operator-proposals" in path and ("approve" in path or "deploy" in path)
                    for path in paths
                )
            with http.open(origin + "/", timeout=2) as response:
                assert response.status == 200
                assert b"Groundwater V7" in response.read()
            for removed in ("/studio/", "/ui/"):
                try:
                    http.open(origin + removed, timeout=2)
                    raise AssertionError(f"Removed legacy route unexpectedly exists: {removed}")
                except urllib.error.HTTPError as error:
                    assert error.code == 404
            try:
                http.open(origin + "/api/v1/projects", timeout=2)
                raise AssertionError("Anonymous project access unexpectedly succeeded")
            except urllib.error.HTTPError as error:
                with error:
                    envelope = json.load(error)
                    if token is None:
                        assert error.code == 503
                        assert envelope["code"] == "DEPENDENCY_UNAVAILABLE"
                        assert envelope["retry_mode"] == "SAME_KEY"
                    else:
                        assert error.code == 401
                        assert envelope["code"] == "AUTHENTICATION_REQUIRED"
            if token:
                with http.open(origin + "/api/v1/ready", timeout=2) as response:
                    assert json.load(response) == {"status": "ready"}

                def call(path, body=None, expected=200, key="installed-smoke"):
                    request = urllib.request.Request(
                        origin + path,
                        data=json.dumps(body).encode() if body is not None else None,
                        headers={
                            "Authorization": "Bearer " + token,
                            "Idempotency-Key": key,
                            "Content-Type": "application/json",
                        },
                    )
                    with http.open(request, timeout=3) as response:
                        assert response.status == expected
                        return json.load(response)

                project = call("/api/v1/projects", {"name": "Installed distribution"}, 201)
                prefix = "/api/v1/projects/" + project["project_id"]
                descriptor = call(prefix + "/authoring/operator-descriptors")
                assert len(descriptor["operators"]) == 11
                draft = call(
                    prefix + "/workflow-definitions",
                    {
                        "expected_revision": 0,
                        "stable_key": "installed-authoring-draft",
                        "metadata": {"title": "Installed authoring draft"},
                        "layout": {},
                    },
                    201,
                    "installed-authoring",
                )
                assert draft["head_revision"] == 1
                body = {
                    "execution_mode": "DIRECT",
                    "spec": {"goal": "HTTP recovery check"},
                    "budget": {"execution_count": 2, "max_in_flight": 1},
                }
                task = call(prefix + "/tasks", body, 201)
                assert call(prefix + "/tasks", body, 201) == task
                task_path = prefix + "/tasks/" + task["task_id"]
                paused = call(task_path + "/pause", {"expected_control_version": 1})
                assert call(paused["status_url"])["command_id"] == paused["command_id"]
                assert call(task_path)["control"] == "PAUSED"
                assert call(task_path + "/budget")["reserved"] == 0
                resumed = call(task_path + "/resume", {"expected_control_version": 2})
                assert call(resumed["status_url"])["status"] == "RESOLVED"
                analysis = call(
                    task_path + "/analysis-revisions",
                    {
                        "expected_spec_revision": 1,
                        "question": "Installed research route",
                        "research_objects": ["installed-object"],
                    },
                    201,
                    "installed-analysis",
                )
                registration = call(
                    task_path + "/analysis-revisions/" + analysis["revision_id"] + "/register",
                    {"expected_spec_revision": 1},
                    201,
                    "installed-registration",
                )
                research = call(task_path + "/research")
                assert (
                    registration["registration_id"]
                    == research["registrations"][0]["registration_id"]
                )
                if legacy:
                    prefix = "/api/v1/projects/" + legacy["project"]
                    replay = call(
                        prefix + "/tasks",
                        {
                            "execution_mode": "DIRECT",
                            "spec": {
                                "goal": "Upgrade with recoverable actions",
                                "requirements": [],
                            },
                            "budget": {"execution_count": 5, "max_in_flight": 3},
                        },
                        201,
                        "legacy-task",
                    )
                    assert replay["task_id"] == legacy["task"]
                    unknown, known = legacy["commands"][1:]
                    pending = call(prefix + "/commands/" + unknown["command_id"])
                    assert pending["status"] == "RECONCILING" and pending.get("result") is None
                    resolved = call(prefix + "/commands/" + known["command_id"])
                    assert resolved["result"]["execution_id"] == "legacy-execution"
                    assert call(prefix + "/tasks/" + legacy["task"] + "/budget")["used"] == 1
            else:
                try:
                    http.open(origin + "/api/v1/ready", timeout=2)
                    raise AssertionError("Unconfigured API incorrectly reported ready")
                except urllib.error.HTTPError as error:
                    assert error.code == 503
        except BaseException:
            log.flush()
            log.seek(0)
            print(log.read(), file=sys.stderr)
            raise
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        log.flush()
        log.seek(0)
        shutdown_log = log.read()
        # Uvicorn completes lifespan shutdown, then re-raises the captured signal.
        assert process.returncode in {0, -signal.SIGTERM}, shutdown_log
        assert "Application shutdown complete." in shutdown_log, shutdown_log
        assert "Finished server process" in shutdown_log, shutdown_log


def smoke_knowledge_server(executable: Path, *, cwd: Path, env: dict[str, str]) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    configured = env | {"V7_KNOWLEDGE_PORT": str(port)}
    http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with (cwd / "knowledge-server.log").open("w+") as log:
        process = subprocess.Popen(
            [str(executable)], cwd=cwd, env=configured, stdout=log, stderr=log
        )
        try:
            deadline = time.monotonic() + 15
            while True:
                if process.poll() is not None:
                    raise RuntimeError("Knowledge service exited during startup")
                try:
                    with http.open(f"http://127.0.0.1:{port}/readyz", timeout=1) as response:
                        assert response.status == 200
                        assert json.load(response) == {"status": "ready"}
                    break
                except urllib.error.URLError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError(
                            "Knowledge service was not ready within 15 seconds"
                        ) from None
                    time.sleep(0.1)
        except BaseException:
            log.flush()
            log.seek(0)
            print(log.read(), file=sys.stderr)
            raise
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        log.flush()
        log.seek(0)
        assert process.returncode in {0, -signal.SIGTERM}, log.read()


def main() -> None:
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required")
    if sys.flags.optimize:
        raise RuntimeError("Run verification without -O; assertions must be enabled")
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("V7_")
        and key
        not in {
            "PYTHONPATH",
            "PYTHONHOME",
            "VIRTUAL_ENV",
            "PYTHONOPTIMIZE",
            "V7_DATABASE_URL",
            "V7_OWNER_TOKEN",
        }
    }
    with tempfile.TemporaryDirectory(prefix="groundwater-v7-p2-") as temp:
        directory = Path(temp)
        studio = directory / "studio"
        run(
            [
                sys.executable,
                ROOT / "scripts/build/build_studio.py",
                "--output",
                studio,
            ],
            cwd=ROOT,
            env=env,
        )
        env["V7_STUDIO_DIST"] = str(studio)
        wheels = directory / "wheels"
        run(
            [
                uv,
                "build",
                "--all-packages",
                "--no-build-isolation",
                "--python",
                sys.executable,
                "--out-dir",
                wheels,
            ],
            cwd=ROOT,
            env=env,
        )
        artifacts = sorted(wheels.glob("*.whl"))
        members = tomllib.loads((ROOT / "pyproject.toml").read_text())["tool"]["uv"]["workspace"][
            "members"
        ]
        assert len(artifacts) == len(members)
        assert len(list(wheels.glob("*.tar.gz"))) == len(members)
        print(f"PASS: built {len(members)} wheels and source distributions", flush=True)

        core_env = directory / "core-env"
        run([uv, "venv", "--python", sys.executable, core_env], cwd=directory, env=env)
        core_python = core_env / "bin/python"
        core_wheel = next(wheels.glob("groundwater_platform_core-*.whl"))
        run(
            [uv, "pip", "install", "--python", core_python, "--no-deps", core_wheel],
            cwd=directory,
            env=env,
        )
        run(
            [
                core_python,
                "-I",
                "-c",
                "from importlib.util import find_spec; "
                "from platform_core.application.health import read_process_health; "
                "from platform_core.application.control import ControlService; "
                "from platform_core.application.dispatch import Dispatcher; "
                "assert read_process_health().status == 'ok'; "
                "assert find_spec('pydantic') is None; assert find_spec('fastapi') is None; "
                "assert find_spec('sqlalchemy') is None",
            ],
            cwd=directory,
            env=env,
        )
        print("PASS: core wheel runs with only the Python standard library", flush=True)

        requirements = directory / "runtime.txt"
        run(
            [
                uv,
                "export",
                "--locked",
                "--no-dev",
                "--no-emit-workspace",
                "--output-file",
                requirements,
            ],
            cwd=ROOT,
            env=env,
        )
        clean_env = directory / "api-env"
        run([uv, "venv", "--python", sys.executable, clean_env], cwd=directory, env=env)
        python = clean_env / "bin/python"
        run(
            [uv, "pip", "install", "--python", python, "--require-hashes", "-r", requirements],
            cwd=directory,
            env=env,
        )
        run(
            [uv, "pip", "install", "--python", python, "--no-deps", *artifacts],
            cwd=directory,
            env=env,
        )
        run([uv, "pip", "check", "--python", python], cwd=directory, env=env)
        run(
            [
                python,
                "-I",
                "-c",
                "import sys, pathlib, platform_api, platform_core, platform_contracts; "
                "import platform_infrastructure, platform_data, platform_execution; "
                "import platform_operators, platform_checkers, platform_knowledge; "
                "import platform_agent_runtime; "
                "assert all(pathlib.Path(m.__file__).is_relative_to(sys.prefix) "
                "for m in (platform_api, platform_core, platform_contracts, "
                "platform_infrastructure, platform_data, platform_execution, "
                "platform_operators, platform_checkers, platform_knowledge, "
                "platform_agent_runtime)); "
                "from platform_agent_runtime.profiles import profile, validate; "
                "assert validate(profile())['adapter'] == 'LANGGRAPH'; "
                "from importlib.util import find_spec; assert find_spec('tests') is None",
            ],
            cwd=directory,
            env=env,
        )
        print(
            "PASS: locked runtime dependencies and wheels installed outside the repository",
            flush=True,
        )
        env.update(_distribution_catalog_environment(directory))
        smoke_server(clean_env / "bin/platform-api", cwd=directory, env=env)
        print(
            "PASS: installed API liveness, unconfigured readiness, OpenAPI and shutdown", flush=True
        )
        with PostgreSQL() as postgres:
            configured = env | {"V7_DATABASE_URL": postgres.url}
            admin = clean_env / "bin/platform-admin"
            for _ in range(2):
                run([admin, "migrate"], cwd=directory, env=configured)
            run([admin, "check-schema"], cwd=directory, env=configured)
            principal = json.loads(
                run([admin, "principal-create", "--name", "smoke"], cwd=directory, env=configured)
            )["principal_id"]
            issued = json.loads(
                run(
                    [admin, "token-issue", "--principal-id", principal],
                    cwd=directory,
                    env=configured,
                )
            )
            smoke_server(
                clean_env / "bin/platform-api", cwd=directory, env=configured, token=issued["token"]
            )
            smoke_worker_lanes(clean_env / "bin/platform-worker", cwd=directory, env=configured)
            cwl_image = (P4_RUNTIME_ROOT / "cwl-image.txt").read_text().strip()
            run(
                [clean_env / "bin/platform-authoring-worker", "--once"],
                cwd=directory,
                env=configured | {"V7_CWL_IMAGE": cwl_image},
            )
            run(
                [admin, "token-revoke", "--token-id", issued["token_id"]],
                cwd=directory,
                env=configured,
            )
        print(
            "PASS: installed migrations, token CLI, control and authoring workers",
            flush=True,
        )
        with PostgreSQL() as postgres:
            knowledge_storage = directory / "knowledge-storage"
            configured = env | {
                "V7_KNOWLEDGE_DATABASE_URL": postgres.url,
                "V7_KNOWLEDGE_STORAGE": str(knowledge_storage),
                "V7_PLATFORM_KNOWLEDGE_TOKEN": "k" * 48,
                # Composition is fail-closed even though the liveness smoke
                # does not perform a Data transfer.
                "V7_DATA_URL": "http://127.0.0.1:1",
                "V7_KNOWLEDGE_DATA_TOKEN": _SMOKE_SERVICE_TOKEN,
            }
            knowledge_admin = clean_env / "bin/knowledge-admin"
            for _ in range(2):
                run([knowledge_admin, "migrate"], cwd=directory, env=configured)
            run([knowledge_admin, "check-schema"], cwd=directory, env=configured)
            smoke_knowledge_server(
                clean_env / "bin/knowledge-service", cwd=directory, env=configured
            )
        print(
            "PASS: installed Knowledge migration, schema check, readiness and shutdown",
            flush=True,
        )
        # Run the full P3 HTTP/container path using only installed wheels and copied test drivers.
        env["V7_TEST_DOCKER_EXECUTABLE"] = str(ROOT.parent / "bin/docker")
        for relative in [
            "tests/__init__.py",
            "tests/platform/database/__init__.py",
            "tests/platform/services/__init__.py",
        ]:
            destination = directory / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("")
        for relative in [
            "tests/platform/services/harness.py",
            "tests/platform/database/processes.py",
            "tests/platform/database/peer_client.py",
        ]:
            shutil.copy2(ROOT / relative, directory / relative)
        shutil.copy2(
            ROOT / "tests/platform/services/installed_driver.py",
            directory / "installed_driver.py",
        )
        distribution_p3_runtime = directory / "runtime/work/p3"
        distribution_p3_runtime.mkdir(parents=True)
        shutil.copy2(P3_RUNTIME_ROOT / "images.json", distribution_p3_runtime / "images.json")
        with PostgreSQL() as postgres:
            configured = env | {"V7_DATABASE_URL": postgres.url}
            run([python, directory / "installed_driver.py"], cwd=directory, env=configured)
        print(
            "PASS: installed Data/Execution migrations, independent workers, "
            "containers and P3 completion",
            flush=True,
        )
        # Repeat from an outside directory through the installed P4 workflow stack.
        (directory / "tests/execution/workflows").mkdir(parents=True)
        (directory / "tests/execution/workflows/__init__.py").write_text("")
        shutil.copy2(
            ROOT / "tests/execution/workflows/installed_driver.py",
            directory / "installed_p4_driver.py",
        )
        distribution_p4_runtime = directory / "runtime/work/p4"
        distribution_p4_runtime.mkdir(parents=True)
        shutil.copy2(P4_RUNTIME_ROOT / "cwl-image.txt", distribution_p4_runtime / "cwl-image.txt")
        with PostgreSQL() as postgres:
            configured = env | {"V7_DATABASE_URL": postgres.url}
            # The driver's own bounded stages can total more than the generic
            # command limit (service readiness + package + execution + review).
            run(
                [python, directory / "installed_p4_driver.py"],
                cwd=directory,
                env=configured,
                timeout=420,
            )
        print(
            "PASS: installed frozen CWL, Toil nodes, atomic publication and independent check",
            flush=True,
        )
        # Repeat the checked GeoPackage -> QGIS -> Native workflow outside the repository.
        (directory / "tests/spatial/native/fixtures").mkdir(parents=True)
        shutil.copy2(
            ROOT / "tests/spatial/native/fixtures/points-utm.gpkg",
            directory / "tests/spatial/native/fixtures/points-utm.gpkg",
        )
        (directory / "examples/workflows").mkdir(parents=True)
        shutil.copy2(
            ROOT / "examples/workflows/qgis-buffer-summary-v1.json",
            directory / "examples/workflows/qgis-buffer-summary-v1.json",
        )
        shutil.copy2(
            ROOT / "tests/spatial/native/installed_driver.py",
            directory / "installed_p42_driver.py",
        )
        (distribution_p4_runtime / "spatial").mkdir()
        shutil.copy2(
            P4_RUNTIME_ROOT / "spatial/images.json",
            distribution_p4_runtime / "spatial/images.json",
        )
        with PostgreSQL() as postgres:
            configured = env | {"V7_DATABASE_URL": postgres.url}
            run([python, directory / "installed_p42_driver.py"], cwd=directory, env=configured)
        print(
            "PASS: installed GeoPackage inspection, QGIS buffer, Native summary and publication",
            flush=True,
        )
        # Repeat the three-node trusted cartography flow from installed wheels.
        shutil.copy2(
            ROOT / "examples/workflows/qgis-buffer-summary-map-v1.json",
            directory / "examples/workflows/qgis-buffer-summary-map-v1.json",
        )
        shutil.copy2(
            ROOT / "tests/spatial/cartography/installed_driver.py",
            directory / "installed_p422_driver.py",
        )
        with PostgreSQL() as postgres:
            configured = env | {"V7_DATABASE_URL": postgres.url}
            run([python, directory / "installed_p422_driver.py"], cwd=directory, env=configured)
        print(
            "PASS: installed trusted QGIS map, PNG delivery and atomic three-node publication",
            flush=True,
        )
        # Test fixture runs with the clean interpreter and installed packages.
        # No production module imports it; no business process spans the upgrade.
        with PostgreSQL() as postgres:
            configured = env | {"V7_DATABASE_URL": postgres.url}
            fixture_path = directory / "legacy.json"
            fixture_script = ROOT / "tests/platform/database/legacy_upgrade.py"
            run([python, fixture_script, "seed", fixture_path], cwd=directory, env=configured)
            for _ in range(2):
                run([admin, "migrate"], cwd=directory, env=configured)
            run([admin, "check-schema"], cwd=directory, env=configured)
            run([python, fixture_script, "verify", fixture_path], cwd=directory, env=configured)
            legacy = json.loads(fixture_path.read_text())
            smoke_server(
                clean_env / "bin/platform-api",
                cwd=directory,
                env=configured,
                token=legacy["token"],
                legacy=legacy,
            )
            smoke_worker_lanes(clean_env / "bin/platform-worker", cwd=directory, env=configured)
        print(
            "PASS: installed 0001 data upgrade, unchanged ledger, known/unknown HTTP commands",
            flush=True,
        )


if __name__ == "__main__":
    main()
