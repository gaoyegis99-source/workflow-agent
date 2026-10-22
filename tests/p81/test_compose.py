import json
import os
import runpy
import subprocess
from argparse import ArgumentTypeError
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ipv4_address = runpy.run_path(str(ROOT / "scripts/configure_p8.py"))["ipv4_address"]


def test_unified_compose_declares_all_services_and_private_bindings(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    values = {
        "V7_P8_PROJECT_NAME": "p81-compose-contract",
        "V7_P8_ENV_FILE": str(tmp_path / "p8.env"),
        "V7_P8_APP_IMAGE": "groundwater-v7-app:test",
        "V7_P8_GATEWAY_BIND": "0.0.0.0",
        "V7_DOCKER_GID": "999",
        "V7_P8_RELEASE_DIR": str(release),
        "V7_P8_POSTGRES_DIR": str(runtime / "postgres"),
        "V7_P8_DATA_DIR": str(runtime / "data"),
        "V7_P8_KNOWLEDGE_DIR": str(runtime / "knowledge"),
        "V7_P8_EXECUTION_DIR": str(runtime / "execution"),
        "V7_P8_SPATIAL_DIR": str(runtime / "spatial"),
        "V7_P8_CONTROL_DIR": str(runtime / "control"),
        "V7_P8_BACKUP_DIR": str(runtime / "backups"),
        "V7_P8_POSTGRES_PASSWORD": "admin-password",
        "V7_PLATFORM_DB_PASSWORD": "platform-password",
        "V7_DATA_DB_PASSWORD": "data-password",
        "V7_EXECUTION_DB_PASSWORD": "execution-password",
        "V7_KNOWLEDGE_DB_PASSWORD": "knowledge-password",
    }
    for path in runtime.iterdir():
        path.mkdir(exist_ok=True)
    Path(values["V7_P8_ENV_FILE"]).write_text(
        "\n".join(f"{key}={value}" for key, value in values.items()) + "\n",
        encoding="utf-8",
    )
    environment = os.environ | values
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            values["V7_P8_ENV_FILE"],
            "-f",
            "deploy/p8.compose.yaml",
            "--profile",
            "ops",
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=True,
    )
    value = json.loads(result.stdout)
    expected = {
        "postgres",
        "volume-init",
        "migrate",
        "platform-api",
        "data-api",
        "execution-api",
        "knowledge-api",
        "gateway",
        "platform-worker",
        "authoring-worker",
        "data-worker",
        "data-document-worker",
        "data-spatial-worker",
        "execution-supervisor",
        "execution-node-1",
        "execution-node-2",
        "execution-publication",
        "knowledge-worker",
        "agent-worker",
        "backup",
    }
    assert set(value["services"]) == expected
    published = {
        service: item.get("ports", [])
        for service, item in value["services"].items()
        if item.get("ports")
    }
    assert set(published) == {"postgres", "execution-api", "gateway"}
    assert published["gateway"][0]["host_ip"] == "0.0.0.0"
    assert published["postgres"][0]["host_ip"] == "127.0.0.1"
    assert published["execution-api"][0]["host_ip"] == "127.0.0.1"
    assert value["services"]["backup"]["profiles"] == ["ops"]
    assert value["services"]["agent-worker"]["command"] == ["agent-worker"]
    worker_services = (
        "platform-worker",
        "authoring-worker",
        "data-worker",
        "data-document-worker",
        "data-spatial-worker",
        "execution-supervisor",
        "execution-node-1",
        "execution-node-2",
        "execution-publication",
        "knowledge-worker",
        "agent-worker",
    )
    for worker in worker_services:
        assert value["services"][worker]["environment"]["V7_WORKER_HEARTBEAT_FILE"] == (
            "/tmp/v7-worker.heartbeat"
        )
        assert value["services"][worker]["healthcheck"]["interval"] == "10s"
        assert "V7_WORKER_HEARTBEAT_FILE" in " ".join(
            value["services"][worker]["healthcheck"]["test"]
        )
    for worker in ("platform-worker", "authoring-worker"):
        assert value["services"][worker]["environment"]["TMPDIR"] == str(
            runtime / "execution" / "platform-tmp"
        )
        sources = {mount["source"] for mount in value["services"][worker]["volumes"]}
        assert str(runtime / "execution") in sources


def test_gateway_bind_accepts_only_ipv4_addresses():
    assert ipv4_address("0.0.0.0") == "0.0.0.0"
    assert ipv4_address("192.168.167.87") == "192.168.167.87"
    with pytest.raises(ArgumentTypeError):
        ipv4_address("all-interfaces")
