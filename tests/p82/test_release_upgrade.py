import json
import runpy
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from platform_contracts.release import P8_DATABASE_SCHEMAS, digest, file_digest, load_release_lock
from platform_contracts.service import ReleaseIdentityMiddleware
from platform_infrastructure.upgrade import (
    Operation,
    install_committed_environment,
    load_env_file,
    validate_upgrade,
    write_target_env,
)

ROOT = Path(__file__).resolve().parents[2]
WHEELS = {
    "groundwater_agent_runtime",
    "groundwater_checker_pack",
    "groundwater_data_service",
    "groundwater_execution_service",
    "groundwater_knowledge_service",
    "groundwater_operator_pack",
    "groundwater_platform_api",
    "groundwater_platform_contracts",
    "groundwater_platform_core",
    "groundwater_platform_infrastructure",
    "groundwater_tabular_operator_pack",
}


def sha(character):
    return "sha256:" + character * 64


def write_json(path, value):
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    return file_digest(path)


def release_pair(tmp_path):
    source, target = tmp_path / "0.15.0-source", tmp_path / "0.15.1-target"
    source.mkdir()
    target.mkdir()
    catalogs = {}
    for index, name in enumerate(
        ("operator-catalog.json", "checker-catalog.json", "tool-catalog.json"), start=1
    ):
        value = {"catalog_digest": sha(str(index)), "catalog_id": name}
        write_json(source / name, value)
        write_json(target / name, value)
        catalogs[name.removesuffix(".json").replace("-", "_")] = {
            "digest": value["catalog_digest"],
            "file_sha256": file_digest(source / name),
        }
    catalogs["tool_catalog"]["catalog_id"] = "tool-catalog.json"
    model_sha = write_json(source / "model-catalog.json", {"model": "old"})
    runtime_sha = write_json(source / "agent-runtime-profile.json", {"runtime": "old"})
    write_json(target / "model-catalog.json", {"model": "new"})
    write_json(target / "agent-runtime-profile.json", {"runtime": "new"})
    images = {
        name: sha(character)
        for name, character in zip(
            (
                "operator",
                "qgis",
                "spatial_native",
                "checker",
                "gdal_checker",
                "cwl",
                "tabular",
                "app",
                "postgres",
            ),
            "abcdef012",
            strict=True,
        )
    }
    source_unsigned = {
        "schema_version": 2,
        "version": "0.15.0",
        **catalogs,
        "images": images,
        "wheels": {f"{name}-0.15.0-py3-none-any.whl": sha("1") for name in WHEELS},
    }
    source_lock = source_unsigned | {"release_digest": digest(source_unsigned)}
    write_json(source / "release-lock.json", source_lock)
    target_catalogs = {
        key: value | {"file_sha256": file_digest(target / (key.replace("_", "-") + ".json"))}
        for key, value in catalogs.items()
    }
    unsigned = {
        "schema_version": 3,
        "version": "0.15.1",
        **target_catalogs,
        "model_catalog": {
            "digest": sha("a"),
            "file_sha256": file_digest(target / "model-catalog.json"),
        },
        "runtime_profile": {
            "digest": sha("b"),
            "file_sha256": file_digest(target / "agent-runtime-profile.json"),
        },
        "images": images,
        "image_refs": {
            name: ("groundwater-v7-app:0.15.1" if name == "app" else f"v7-{name}:0.15.1")
            for name in images
        },
        "source_tree": {"digest": sha("c"), "file_count": 1},
        "wheels": {f"{name}-0.15.1-py3-none-any.whl": sha("2") for name in WHEELS},
        "web_assets": {"index.html": sha("3")},
        "dependency_wheels": {"dependency.whl": sha("4")},
        "compatibility": {
            "upgrade_from": ["0.15.0"],
            "api_contract": "api/v1",
            "backup_formats": [1, 2],
            "database_schemas": P8_DATABASE_SCHEMAS,
            "recovery_releases": {
                "0.15.0": {
                    "release_digest": source_lock["release_digest"],
                    "catalogs": {key: value["digest"] for key, value in catalogs.items()},
                    "images": images,
                    "model_catalog": model_sha,
                    "runtime_profile": runtime_sha,
                }
            },
        },
        "runtime_requirements": {
            "docker": ">=27,<30",
            "compose": ">=2.30,<3",
            "postgresql": "17",
            "python": ">=3.12,<3.13",
        },
    }
    target_lock = unsigned | {"release_digest": digest(unsigned)}
    write_json(target / "release-lock.json", target_lock)
    return source, source_lock, target, target_lock


def test_v3_release_and_exact_n_minus_one_compatibility(tmp_path):
    source, source_lock, target, target_lock = release_pair(tmp_path)
    assert (
        load_release_lock(target / "release-lock.json", target_lock["release_digest"])
        == target_lock
    )
    plan = validate_upgrade(source, target)
    assert plan["source_release_digest"] == source_lock["release_digest"]
    assert plan["target_release_digest"] == target_lock["release_digest"]
    assert plan["database_schemas"] == P8_DATABASE_SCHEMAS

    changed = json.loads((target / "release-lock.json").read_text())
    changed["compatibility"]["recovery_releases"]["0.15.0"]["images"]["cwl"] = sha("9")
    changed["release_digest"] = digest(
        {key: value for key, value in changed.items() if key != "release_digest"}
    )
    write_json(target / "release-lock.json", changed)
    with pytest.raises(ValueError, match="image set"):
        validate_upgrade(source, target)


def test_operation_state_is_durable_and_cannot_roll_back_after_commit(tmp_path):
    source, _, target, _ = release_pair(tmp_path)
    env = tmp_path / "source.env"
    env.write_text(f"V7_P8_RELEASE_DIR={source}\nSECRET=value\n", encoding="utf-8")
    env.chmod(0o600)
    plan = validate_upgrade(source, target)
    operation = Operation(tmp_path / "operation")
    record = operation.create(plan, env)
    assert record["source_env_mode"] == "0o600"
    snapshot = Path(record["source_env_snapshot"])
    assert snapshot.read_bytes() == env.read_bytes()
    assert snapshot.stat().st_mode & 0o777 == 0o600
    for state in (
        "BARRIER_ACTIVE",
        "PARKED",
        "BACKED_UP",
        "TARGET_INSTALLED",
        "TARGET_VERIFIED",
        "COMMITTED",
    ):
        record = operation.transition(state)
    assert record["state"] == "COMMITTED"
    with pytest.raises(RuntimeError, match="Invalid upgrade transition"):
        operation.transition("ROLLING_BACK")
    lines = operation.events_path.read_text().splitlines()
    assert len(lines) == 7
    assert json.loads(lines[-1])["state"] == "COMMITTED"


def test_committed_target_environment_atomically_becomes_canonical(tmp_path):
    source, _, target, target_lock = release_pair(tmp_path)
    env = tmp_path / "source.env"
    env.write_text(
        "\n".join(
            (
                f"V7_P8_RELEASE_DIR={source}",
                "V7_P8_APP_IMAGE=groundwater-v7-app:0.15.0",
                "V7_P8_PROJECT_NAME=groundwater-v7-p8-source",
                f"V7_P8_CONTROL_DIR={tmp_path / 'control'}",
                "V7_PLATFORM_DB_PASSWORD=secret",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    env.chmod(0o600)
    operation = Operation(tmp_path / "operation")
    operation.create(validate_upgrade(source, target), env)
    write_target_env(env, target, operation.root / "target.env")
    with pytest.raises(RuntimeError, match="only be installed after COMMITTED"):
        install_committed_environment(operation.root)
    for state in (
        "BARRIER_ACTIVE",
        "PARKED",
        "BACKED_UP",
        "TARGET_INSTALLED",
        "TARGET_VERIFIED",
        "COMMITTED",
    ):
        operation.transition(state)

    installed = install_committed_environment(operation.root)
    values = load_env_file(env)
    assert installed["status"] == "installed"
    assert values["V7_RELEASE_DIGEST"] == target_lock["release_digest"]
    assert values["V7_P8_RELEASE_DIR"] == str(target.resolve())
    assert values["V7_P8_ENV_FILE"] == str(env.resolve())
    assert values["V7_PLATFORM_DB_PASSWORD"] == "secret"
    assert install_committed_environment(operation.root)["status"] == "already-installed"


def test_target_environment_preserves_credentials_and_switches_release(tmp_path):
    source, _, target, target_lock = release_pair(tmp_path)
    env = tmp_path / "source.env"
    env.write_text(
        "\n".join(
            (
                f"V7_P8_RELEASE_DIR={source}",
                "V7_P8_APP_IMAGE=groundwater-v7-app:0.15.0",
                "V7_P8_PROJECT_NAME=groundwater-v7-p8-source",
                f"V7_P8_CONTROL_DIR={tmp_path / 'control'}",
                "V7_PLATFORM_DB_PASSWORD=secret",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    env.chmod(0o600)
    output = tmp_path / "target" / "target.env"
    write_target_env(env, target, output)
    values = load_env_file(output)
    assert values["V7_PLATFORM_DB_PASSWORD"] == "secret"
    assert values["V7_RELEASE_DIGEST"] == target_lock["release_digest"]
    assert values["V7_P8_APP_IMAGE"] == "groundwater-v7-app:0.15.1"
    assert values["V7_P8_ENV_FILE"] == str(output.resolve())
    assert values["V7_DEPLOYMENT_ID"] == "groundwater-v7-p8-source"
    assert values["V7_PARK_MANIFEST"] == str(tmp_path / "control" / "park-manifest.json")
    assert output.stat().st_mode & 0o777 == 0o600

    second = tmp_path / "target" / "operation.env"
    park = tmp_path / "control" / "park-manifests" / "operation.json"
    write_target_env(env, target, second, park_manifest=park)
    assert load_env_file(second)["V7_PARK_MANIFEST"] == str(park.resolve())


def test_internal_release_header_rejects_mixed_services(monkeypatch):
    monkeypatch.setenv("V7_DEPLOYMENT_MODE", "production")
    monkeypatch.setenv("V7_RELEASE_DIGEST", sha("a"))
    app = FastAPI()
    app.add_middleware(ReleaseIdentityMiddleware)

    @app.get("/internal/fact")
    def internal():
        return {"ok": True}

    @app.get("/internal/workflows/execution/nodes/node")
    def legacy_workflow_bridge():
        # The real route additionally checks its frozen high-entropy bridge
        # credential, Action identity and engine epoch.
        return {"ok": True}

    @app.get("/api/v1/fact")
    def public():
        return {"ok": True}

    with TestClient(app) as client:
        assert client.get("/api/v1/fact").status_code == 200
        missing = client.get("/internal/fact")
        assert missing.status_code == 503
        assert missing.json()["code"] == "RELEASE_MISMATCH"
        correct = client.get("/internal/fact", headers={"X-V7-Release-Digest": sha("a")})
        assert correct.status_code == 200
        assert correct.headers["X-V7-Release-Digest"] == sha("a")
        legacy = client.get("/internal/workflows/execution/nodes/node")
        assert legacy.status_code == 200
        wrong_legacy = client.get(
            "/internal/workflows/execution/nodes/node",
            headers={"X-V7-Release-Digest": sha("b")},
        )
        assert wrong_legacy.status_code == 503


def test_upgrade_scripts_are_syntactically_valid_and_do_not_overwrite_release():
    subprocess.run(["bash", "-n", "scripts/p8-upgrade.sh"], cwd=ROOT, check=True)
    subprocess.run(["bash", "-n", "scripts/build_p8_release.sh"], cwd=ROOT, check=True)
    source = (ROOT / "scripts/build_p8_release.sh").read_text()
    assert "Refusing to overwrite immutable release" in source
    assert 'find "$release_dir" -type d -exec chmod 0555' in source
    assert 'rm -rf "$release_dir"' not in source
    upgrade = (ROOT / "scripts/p8-upgrade.sh").read_text()
    assert "trap automatic_rollback ERR" in upgrade
    assert upgrade.index('transition "$operation" COMMITTED') < upgrade.index(
        'install-committed-environment "$operation"'
    )
    assert upgrade.index('install-committed-environment "$operation"') < upgrade.index(
        'rm -f -- "$V7_MAINTENANCE_FILE"'
    )


def test_runtime_image_source_manifest_excludes_generated_trees():
    module = runpy.run_path(str(ROOT / "scripts/release_source_manifest.py"))
    paths = {path.relative_to(ROOT).as_posix() for path in module["source_files"]()}
    assert not any("/node_modules/" in f"/{path}/" for path in paths)
    assert not any("/.vite/" in f"/{path}/" for path in paths)
    assert not any("/.vitest-cache/" in f"/{path}/" for path in paths)
