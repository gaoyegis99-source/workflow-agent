"""Static direct-Compose topology and least-privilege regression tests.

These tests intentionally never invoke Docker.  The project daemon may be
offline during source validation, so Compose rendering through a host daemon is
not an acceptable substitute for inspecting the declared topology.
"""

from __future__ import annotations

import re
import subprocess
import sys

import pytest
import yaml
from platform_infrastructure.cold_backup import CONFIG_ENV, configuration_from_environment

from scripts.configure import configure_p8 as configure
from scripts.operations.p8_preflight import (
    SERVICE_ENVIRONMENT_KEYS,
    PreflightRefusal,
    validate_control_environment,
)
from tests.pathing import SOURCE_ROOT

ROOT = SOURCE_ROOT
COMPOSE = ROOT / "deploy/production/p8.compose.yaml"
APP_DOCKERFILE = ROOT / "deploy/production/app.Dockerfile"
TEST_LABEL_OVERLAY = ROOT / "deploy/production/p8.test-labels.compose.yaml"
TEST_STACK = ROOT / "scripts/verification/p8-test-stack.sh"
DIGEST = "sha256:" + "a" * 64


def _values() -> dict[str, str]:
    token = "t" * 40
    return {
        "V7_DEPLOYMENT_MODE": "direct",
        "V7_DEPLOYMENT_ID": "unit-test",
        "V7_DEPLOYMENT_REVISION": "sha256:" + "b" * 64,
        "V7_IDENTITY_MODE": "token",
        "V7_OPERATOR_CATALOG": "/artifact/operator-catalog.json",
        "V7_CHECKER_CATALOG": "/artifact/checker-catalog.json",
        "V7_AGENT_TOOL_CATALOG": "/artifact/tool-catalog.json",
        "V7_MODEL_CATALOG": "/artifact/model-catalog.json",
        "V7_AGENT_RUNTIME_PROFILE": "/artifact/agent-runtime-profile.json",
        "V7_OPERATOR_IMAGE": DIGEST,
        "V7_CHECKER_IMAGE": DIGEST,
        "V7_GDAL_CHECKER_IMAGE": DIGEST,
        "V7_SPATIAL_QGIS_IMAGE_ID": DIGEST,
        "V7_SPATIAL_NATIVE_IMAGE_ID": DIGEST,
        "V7_TABULAR_OPERATOR_IMAGE": DIGEST,
        "V7_CWL_IMAGE": DIGEST,
        "V7_PLATFORM_URL": "http://platform-api:8007",
        "V7_PLATFORM_UPSTREAM": "http://platform-api:8007",
        "V7_DATA_URL": "http://data-api:8011",
        "V7_EXECUTION_URL": "http://execution-api:8012",
        "V7_KNOWLEDGE_URL": "http://knowledge-api:8013",
        "V7_EXECUTION_CALLBACK_URL": "http://execution-api:8012",
        "V7_EXECUTION_WORKLOAD_NETWORK": "groundwater-v7-p8-unit-test-v7-workload",
        "V7_DATABASE_URL": "postgresql+psycopg://platform@postgres/v7_platform",
        "V7_AGENT_CHECKPOINT_DATABASE_URL": "postgresql+psycopg://platform@postgres/v7_platform",
        "V7_DATA_DATABASE_URL": "postgresql+psycopg://data@postgres/v7_data",
        "V7_EXECUTION_DATABASE_URL": "postgresql+psycopg://execution@postgres/v7_execution",
        "V7_KNOWLEDGE_DATABASE_URL": "postgresql+psycopg://knowledge@postgres/v7_knowledge",
        "V7_PLATFORM_DATA_TOKEN": token,
        "V7_PLATFORM_EXECUTION_TOKEN": token,
        "V7_PLATFORM_KNOWLEDGE_TOKEN": token,
        "V7_EXECUTION_DATA_TOKEN": token,
        "V7_EXECUTION_PLATFORM_TOKEN": token,
        "V7_KNOWLEDGE_DATA_TOKEN": token,
        "V7_P8_POSTGRES_PASSWORD": token,
        "V7_PLATFORM_DB_PASSWORD": token,
        "V7_DATA_DB_PASSWORD": token,
        "V7_EXECUTION_DB_PASSWORD": token,
        "V7_KNOWLEDGE_DB_PASSWORD": token,
        "V7_DATA_STORAGE": "/runtime/data",
        "V7_KNOWLEDGE_STORAGE": "/runtime/knowledge",
        "V7_EXECUTION_WORKDIR": "/runtime/execution",
        "V7_DATA_SPATIAL_WORK_ROOT": "/runtime/spatial",
        "V7_MAINTENANCE_FILE": "/runtime/control/maintenance",
        "V7_PARK_MANIFEST": "/runtime/control/park-manifest.json",
        "V7_DATA_HOST": "0.0.0.0",
        "V7_DATA_PORT": "8011",
        "V7_EXECUTION_HOST": "0.0.0.0",
        "V7_EXECUTION_PORT": "8012",
        "V7_KNOWLEDGE_HOST": "0.0.0.0",
        "V7_KNOWLEDGE_PORT": "8013",
        "DEEPSEEK_API_KEY": token,
    }


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def test_direct_compose_declares_network_roles_and_socket_whitelist():
    value = _compose()
    services = value["services"]
    assert set(value["networks"]) == {
        "v7-ingress",
        "v7-service",
        "v7-database",
        "v7-workload",
        "v7-egress",
    }
    assert value["networks"]["v7-ingress"]["driver_opts"] == {
        "com.docker.network.bridge.enable_ip_masquerade": "false"
    }
    assert value["networks"]["v7-workload"]["internal"] is True
    assert services["gateway"]["networks"] == ["v7-service", "v7-ingress"]
    assert {
        name for name, service in services.items() if "v7-ingress" in service.get("networks", [])
    } == {"gateway"}
    assert services["execution-api"]["networks"] == ["v7-service", "v7-database", "v7-workload"]
    assert "v7-workload" in services["execution-supervisor"]["networks"]
    assert "v7-workload" in services["execution-node-1"]["networks"]
    assert "v7-workload" in services["execution-publication"]["networks"]
    assert services["knowledge-api"]["networks"][-1] == "v7-egress"
    assert services["agent-worker"]["networks"][-1] == "v7-egress"
    assert all(service.get("network_mode") != "host" for service in services.values())

    allowed_socket = {
        "platform-worker-package",
        "authoring-worker",
        "data-spatial-worker",
        "execution-supervisor",
        "execution-node-1",
        "execution-node-2",
        "execution-publication",
        "backup",
    }
    mounted_socket = {
        name
        for name, service in services.items()
        if any(str(mount).endswith(":/var/run/docker.sock") for mount in service.get("volumes", []))
    }
    assert mounted_socket == allowed_socket
    socket_groups = {name for name, service in services.items() if service.get("group_add")}
    assert socket_groups == allowed_socket
    lanes = (
        "platform-worker-action",
        "platform-worker-acceptance",
        "platform-worker-knowledge",
    )
    for name in lanes:
        assert name not in mounted_socket
        assert not services[name].get("group_add")
        assert all(
            "V7_P8_EXECUTION_DIR" not in str(mount) for mount in services[name].get("volumes", [])
        )


def test_lock_era_build_and_upgrade_entrypoints_fail_closed():
    commands = (
        (sys.executable, ROOT / "scripts/release/build_p7_release_lock.py"),
        ("bash", ROOT / "scripts/release/build_p8_release.sh"),
        ("bash", ROOT / "scripts/operations/p8-upgrade.sh"),
    )
    for command in commands:
        result = subprocess.run(command, text=True, capture_output=True, check=False)
        assert result.returncode == 64
        assert "RETIRED_RELEASE_LOCK" in result.stderr


def test_direct_compose_hardens_regular_services_and_does_not_publish_internal_ports():
    services = _compose()["services"]
    exceptions = {"postgres", "volume-init", "migrate", "backup"}
    for name, service in services.items():
        if name in exceptions:
            assert "io.platform.hardening-exception" in service["labels"]
            continue
        assert service["read_only"] is True
        assert service["cap_drop"] == ["ALL"]
        assert "no-new-privileges:true" in service["security_opt"]
        assert service["tmpfs"] == ["/tmp:rw,noexec,nosuid,size=64m"]
    port_publishers = {name for name, service in services.items() if service.get("ports")}
    assert port_publishers == {"gateway"}
    assert "USER 65532:65532" in APP_DOCKERFILE.read_text(encoding="utf-8")


def test_volume_initializer_defers_service_environment_expansion_to_the_container():
    services = _compose()["services"]
    command = services["volume-init"]["command"][-1]
    for name in (
        "V7_DATA_STORAGE",
        "V7_KNOWLEDGE_STORAGE",
        "V7_EXECUTION_WORKDIR",
        "V7_DATA_SPATIAL_WORK_ROOT",
        "V7_MAINTENANCE_FILE",
    ):
        assert f"$${name}" in command
        assert f'"${name}"' not in command
    assert "chown" not in command
    assert "chmod 0777" in command
    assert "chmod 1777" in command
    assert (
        "./init-db.sh:/docker-entrypoint-initdb.d/10-v7-services.sh:ro"
        in services["postgres"]["volumes"]
    )


def test_p8_test_label_overlay_does_not_reintroduce_a_default_network():
    overlay = yaml.safe_load(TEST_LABEL_OVERLAY.read_text(encoding="utf-8"))
    assert set(overlay["networks"]) == {
        "v7-ingress",
        "v7-service",
        "v7-database",
        "v7-workload",
        "v7-egress",
    }
    launcher = TEST_STACK.read_text(encoding="utf-8")
    assert "V7_P8_PROJECT_NAME" in launcher
    assert 'export V7_DEPLOYMENT_ID="$instance"' in launcher


def test_service_environment_scopes_do_not_leak_unrelated_credentials():
    environments = configure._service_environments(_values(), proxy_token="")
    assert "V7_DATABASE_URL" not in environments["gateway"]
    gateway_secret_names = {
        key for key in environments["gateway"] if key.endswith(("_PASSWORD", "_DATABASE_URL"))
    }
    assert not gateway_secret_names
    for lane in (
        "platform-worker-action",
        "platform-worker-acceptance",
        "platform-worker-knowledge",
    ):
        assert "V7_EXECUTION_WORKDIR" not in environments[lane]
        assert "DEEPSEEK_API_KEY" not in environments[lane]
        assert "V7_DATA_DATABASE_URL" not in environments[lane]
        assert "V7_KNOWLEDGE_DATABASE_URL" not in environments[lane]
    assert set(environments["platform-worker-package"]) == {
        "V7_DEPLOYMENT_MODE",
        "V7_DEPLOYMENT_ID",
        "V7_DEPLOYMENT_REVISION",
        "V7_DATABASE_URL",
        "V7_OPERATOR_CATALOG",
        "V7_CWL_IMAGE",
    }
    assert "DEEPSEEK_API_KEY" in environments["agent-worker"]
    assert "DEEPSEEK_API_KEY" not in environments["backup"]
    assert "V7_PLATFORM_EXECUTION_TOKEN" not in environments["backup"]
    for worker in ("data-worker", "data-document-worker"):
        assert "V7_SPATIAL_QGIS_IMAGE_ID" not in environments[worker]
        assert "V7_DATA_SPATIAL_WORK_ROOT" not in environments[worker]
        assert not {
            key
            for key in environments[worker]
            if key.endswith("_TOKEN") or key.endswith("_PASSWORD")
        }
    assert "V7_SPATIAL_QGIS_IMAGE_ID" not in environments["data-api"]
    assert "V7_DATA_SPATIAL_WORK_ROOT" not in environments["data-api"]
    assert environments["data-spatial-worker"]["V7_SPATIAL_QGIS_IMAGE_ID"] == DIGEST
    assert "V7_DATA_SPATIAL_WORK_ROOT" in environments["data-spatial-worker"]
    assert set(environments["migrate"]) == {
        "V7_DEPLOYMENT_MODE",
        "V7_DEPLOYMENT_ID",
        "V7_DEPLOYMENT_REVISION",
        "V7_DATABASE_URL",
        "V7_AGENT_CHECKPOINT_DATABASE_URL",
        "V7_DATA_DATABASE_URL",
        "V7_EXECUTION_DATABASE_URL",
        "V7_KNOWLEDGE_DATABASE_URL",
    }


def test_configurator_generates_nonsecret_control_and_complete_service_environments(
    tmp_path, monkeypatch
):
    project = tmp_path / "groundwater-v7"
    artifact = project / "artifacts" / "candidate"
    artifact.mkdir(parents=True)
    for name in configure.ARTIFACT_FILES:
        (artifact / name).write_text("{}\n", encoding="utf-8")
    runtime = project / "runtime" / "p8"
    runtime.mkdir(parents=True)
    socket = project / "runtime" / "docker.sock"
    socket.write_text("not-a-real-socket", encoding="utf-8")
    monkeypatch.setattr(configure, "PROJECT_ROOT", project)
    monkeypatch.setattr(configure, "P8_RUNTIME_ROOT", runtime)
    monkeypatch.setattr(configure, "_inspect_image", lambda _image: None)
    monkeypatch.setattr(configure, "require_socket", lambda: socket)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "configure_p8.py",
            "--instance",
            "unit",
            "--runtime-root",
            str(runtime),
            "--artifact-dir",
            str(artifact),
            "--app-image",
            "groundwater-v7-app:test",
            "--operator-image",
            DIGEST,
            "--checker-image",
            DIGEST,
            "--gdal-checker-image",
            DIGEST,
            "--spatial-qgis-image",
            DIGEST,
            "--spatial-native-image",
            DIGEST,
            "--tabular-image",
            DIGEST,
            "--cwl-image",
            DIGEST,
        ],
    )
    configure.main()
    instance = runtime / "instances" / "unit"
    control = (instance / "compose.env").read_text(encoding="utf-8")
    assert "PASSWORD" not in control and "TOKEN" not in control and "DATABASE_URL" not in control
    service_files = sorted((instance / "environment").glob("*.env"))
    assert len(service_files) == 22
    assert all(path.stat().st_mode & 0o077 == 0 for path in service_files)
    referenced = set(re.findall(r"\$\{(V7_P8_[A-Z0-9_]+_ENV_FILE):", COMPOSE.read_text()))
    control_keys = {line.split("=", 1)[0] for line in control.splitlines() if "=" in line}
    assert referenced <= control_keys


@pytest.mark.parametrize("failure", ("missing-artifact", "outside-artifact", "symlink-artifact"))
def test_configurator_refuses_invalid_artifact_inputs(tmp_path, monkeypatch, failure):
    project = tmp_path / "groundwater-v7"
    runtime = project / "runtime" / "p8"
    runtime.mkdir(parents=True)
    good = project / "artifact"
    good.mkdir()
    for name in configure.ARTIFACT_FILES:
        (good / name).write_text("{}\n", encoding="utf-8")
    if failure == "missing-artifact":
        (good / "operator-catalog.json").unlink()
        artifact = good
    elif failure == "outside-artifact":
        artifact = tmp_path / "other-artifact"
        artifact.mkdir()
    else:
        artifact = project / "artifact-link"
        artifact.symlink_to(good, target_is_directory=True)
    monkeypatch.setattr(configure, "PROJECT_ROOT", project)
    monkeypatch.setattr(configure, "P8_RUNTIME_ROOT", runtime)
    monkeypatch.setattr(configure, "_inspect_image", lambda _image: None)
    monkeypatch.setattr(configure, "require_socket", lambda: project / "socket")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "configure_p8.py",
            "--artifact-dir",
            str(artifact),
            "--app-image",
            "test",
            "--operator-image",
            DIGEST,
            "--checker-image",
            DIGEST,
            "--gdal-checker-image",
            DIGEST,
            "--spatial-qgis-image",
            DIGEST,
            "--spatial-native-image",
            DIGEST,
            "--tabular-image",
            DIGEST,
            "--cwl-image",
            DIGEST,
        ],
    )
    with pytest.raises(SystemExit):
        configure.main()


def test_science_image_digest_must_exist_in_the_project_daemon(monkeypatch):
    def unavailable(_image):
        raise ValueError("APPLICATION_IMAGE_UNAVAILABLE")

    monkeypatch.setattr(configure, "_inspect_image", unavailable)
    with pytest.raises(ValueError, match="APPLICATION_IMAGE_UNAVAILABLE"):
        configure._science_image(DIGEST, "V7_CWL_IMAGE")


def test_rootless_socket_group_is_translated_into_the_container_namespace(tmp_path):
    subgid = tmp_path / "subgid"
    subgid.write_text("operator:100000:65536\n", encoding="utf-8")
    assert (
        configure._container_group_id(
            100996,
            username="operator",
            primary_gid=1000,
            subordinate_file=subgid,
        )
        == 997
    )
    assert (
        configure._container_group_id(
            1000,
            username="operator",
            primary_gid=1000,
            subordinate_file=subgid,
        )
        == 0
    )
    with pytest.raises(ValueError, match="DOCKER_SOCKET_GID_UNMAPPED"):
        configure._container_group_id(
            99999,
            username="operator",
            primary_gid=1000,
            subordinate_file=subgid,
        )


def test_preflight_rejects_missing_and_outside_service_environment_files(tmp_path):
    project = tmp_path / "groundwater-v7"
    instance = project / "runtime/p8/instances/fixture"
    environment = instance / "environment"
    environment.mkdir(parents=True)
    control = instance / "compose.env"
    entries = []
    for index, key in enumerate(SERVICE_ENVIRONMENT_KEYS):
        service = environment / f"{index}.env"
        service.write_text("V7_DEPLOYMENT_ID=fixture\n", encoding="utf-8")
        service.chmod(0o600)
        entries.append(f"{key}={service}")
    control.write_text("\n".join(entries) + "\n", encoding="utf-8")
    control.chmod(0o600)
    assert validate_control_environment(project, control)[SERVICE_ENVIRONMENT_KEYS[0]].endswith(
        "0.env"
    )

    (environment / "0.env").unlink()
    with pytest.raises(PreflightRefusal, match="P8_SERVICE_ENVIRONMENT_INVALID"):
        validate_control_environment(project, control)

    replacement = environment / "0.env"
    replacement.write_text("V7_DEPLOYMENT_ID=fixture\n", encoding="utf-8")
    replacement.chmod(0o600)
    outside = tmp_path / "outside.env"
    outside.write_text("V7_DEPLOYMENT_ID=outside\n", encoding="utf-8")
    outside.chmod(0o600)
    control.write_text(
        "\n".join(
            f"{key}={outside if index == 0 else environment / f'{index}.env'}"
            for index, key in enumerate(SERVICE_ENVIRONMENT_KEYS)
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(PreflightRefusal, match="P8_SERVICE_ENVIRONMENT_OUTSIDE_PROJECT"):
        validate_control_environment(project, control)


def test_preflight_rejects_secret_material_in_the_control_environment(tmp_path):
    project = tmp_path / "groundwater-v7"
    instance = project / "runtime/p8/instances/fixture"
    environment = instance / "environment"
    environment.mkdir(parents=True)
    control = instance / "compose.env"
    for index, _key in enumerate(SERVICE_ENVIRONMENT_KEYS):
        service = environment / f"{index}.env"
        service.write_text("V7_DEPLOYMENT_ID=fixture\n", encoding="utf-8")
        service.chmod(0o600)
    control.write_text(
        "V7_DATABASE_URL=postgresql://must-not-be-here\n"
        + "\n".join(
            f"{key}={environment / f'{index}.env'}"
            for index, key in enumerate(SERVICE_ENVIRONMENT_KEYS)
        )
        + "\n",
        encoding="utf-8",
    )
    control.chmod(0o600)
    with pytest.raises(PreflightRefusal, match="P8_CONTROL_ENVIRONMENT_CONTAINS_SECRET"):
        validate_control_environment(project, control)


def test_cold_backup_uses_direct_deployment_metadata_not_a_release_lock(tmp_path, monkeypatch):
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    for filename in CONFIG_ENV:
        (artifact / filename).write_text("{}\n", encoding="utf-8")
    values = {
        "V7_DEPLOYMENT_ID": "backup-test",
        "V7_DEPLOYMENT_REVISION": "sha256:" + "c" * 64,
        "V7_DATABASE_URL": "postgresql+psycopg://platform@postgres/v7_platform",
        "V7_DATA_DATABASE_URL": "postgresql+psycopg://data@postgres/v7_data",
        "V7_EXECUTION_DATABASE_URL": "postgresql+psycopg://execution@postgres/v7_execution",
        "V7_KNOWLEDGE_DATABASE_URL": "postgresql+psycopg://knowledge@postgres/v7_knowledge",
        "V7_DATA_STORAGE": str(tmp_path / "data"),
        "V7_KNOWLEDGE_STORAGE": str(tmp_path / "knowledge"),
        "V7_EXECUTION_WORKDIR": str(tmp_path / "execution"),
        "V7_DATA_SPATIAL_WORK_ROOT": str(tmp_path / "spatial"),
    } | {variable: str(artifact / filename) for filename, variable in CONFIG_ENV.items()}
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("V7_RELEASE_LOCK", raising=False)
    monkeypatch.delenv("V7_RELEASE_DIGEST", raising=False)
    configuration = configuration_from_environment()
    assert configuration.deployment_id == "backup-test"
    assert configuration.deployment_revision == "sha256:" + "c" * 64
