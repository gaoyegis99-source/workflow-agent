"""Build/install wheels outside the source tree and smoke-test a real HTTP process.

Run using `uv run --locked python scripts/verify_distribution.py` after `uv sync --locked`.
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
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

from p2_environment import PostgreSQL

ROOT = Path(__file__).resolve().parents[1]


def run(args: list[str | Path], *, cwd: Path, env: dict[str, str]) -> str:
    result = subprocess.run(
        [str(arg) for arg in args], cwd=cwd, env=env, text=True, capture_output=True, timeout=180
    )
    if result.returncode:
        raise RuntimeError(f"Command failed: {args}\n{result.stdout}\n{result.stderr}")
    return result.stdout


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
            with http.open(origin + "/api/v1/openapi.json", timeout=2) as response:
                schema = json.load(response)
                assert schema["info"]["description"].startswith("P8.2 / A7:")
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
                    assert error.code == 401
                    assert json.load(error)["code"] == "AUTHENTICATION_REQUIRED"
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
            run([clean_env / "bin/platform-worker", "--once"], cwd=directory, env=configured)
            cwl_image = (ROOT / ".p4-runtime/cwl-image.txt").read_text().strip()
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
        for relative in ["tests/__init__.py", "tests/p2/__init__.py", "tests/p3/__init__.py"]:
            destination = directory / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text("")
        for relative in ["tests/p3/harness.py", "tests/p2/processes.py", "tests/p2/peer_client.py"]:
            shutil.copy2(ROOT / relative, directory / relative)
        shutil.copy2(ROOT / "tests/p3/installed_driver.py", directory / "installed_driver.py")
        (directory / ".p3-runtime").mkdir()
        shutil.copy2(ROOT / ".p3-runtime/images.json", directory / ".p3-runtime/images.json")
        with PostgreSQL() as postgres:
            configured = env | {"V7_DATABASE_URL": postgres.url}
            run([python, directory / "installed_driver.py"], cwd=directory, env=configured)
        print(
            "PASS: installed Data/Execution migrations, independent workers, "
            "containers and P3 completion",
            flush=True,
        )
        # Repeat from an outside directory through the installed P4 workflow stack.
        (directory / "tests/p4").mkdir()
        (directory / "tests/p4/__init__.py").write_text("")
        shutil.copy2(ROOT / "tests/p4/installed_driver.py", directory / "installed_p4_driver.py")
        (directory / ".p4-runtime").mkdir()
        shutil.copy2(ROOT / ".p4-runtime/cwl-image.txt", directory / ".p4-runtime/cwl-image.txt")
        with PostgreSQL() as postgres:
            configured = env | {"V7_DATABASE_URL": postgres.url}
            run([python, directory / "installed_p4_driver.py"], cwd=directory, env=configured)
        print(
            "PASS: installed frozen CWL, Toil nodes, atomic publication and independent check",
            flush=True,
        )
        # Repeat the checked GeoPackage -> QGIS -> Native workflow outside the repository.
        (directory / "tests/p42/fixtures").mkdir(parents=True)
        shutil.copy2(
            ROOT / "tests/p42/fixtures/points-utm.gpkg",
            directory / "tests/p42/fixtures/points-utm.gpkg",
        )
        (directory / "examples/workflows").mkdir(parents=True)
        shutil.copy2(
            ROOT / "examples/workflows/qgis-buffer-summary-v1.json",
            directory / "examples/workflows/qgis-buffer-summary-v1.json",
        )
        shutil.copy2(ROOT / "tests/p42/installed_driver.py", directory / "installed_p42_driver.py")
        (directory / ".p4-runtime/spatial").mkdir()
        shutil.copy2(
            ROOT / ".p4-runtime/spatial/images.json",
            directory / ".p4-runtime/spatial/images.json",
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
            ROOT / "tests/p422/installed_driver.py", directory / "installed_p422_driver.py"
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
            fixture_script = ROOT / "tests/p2/legacy_upgrade.py"
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
            run([clean_env / "bin/platform-worker", "--once"], cwd=directory, env=configured)
        print(
            "PASS: installed 0001 data upgrade, unchanged ledger, known/unknown HTTP commands",
            flush=True,
        )


if __name__ == "__main__":
    main()
