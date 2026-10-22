#!/usr/bin/env python3
"""Create one non-overwriting P8 deployment instance configuration."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import secrets
import subprocess
from pathlib import Path

from platform_contracts.release import load_release_lock

ROOT = Path(__file__).resolve().parents[1]
IDENTIFIER = re.compile(r"[a-z][a-z0-9-]{0,31}")
POSTGRES_IMAGE = "sha256:3645570cccdfa447589da9f57dd740faa29b30938e861289a5574b6ca6b03826"


def ipv4_address(value: str) -> str:
    try:
        return str(ipaddress.IPv4Address(value))
    except ipaddress.AddressValueError as error:
        raise argparse.ArgumentTypeError("must be an IPv4 address") from error


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--instance", default="local")
    parser.add_argument("--release-dir", type=Path, required=True)
    parser.add_argument("--app-image")
    parser.add_argument("--gateway-bind", type=ipv4_address, default="127.0.0.1")
    parser.add_argument("--gateway-port", type=int, default=18080)
    parser.add_argument("--execution-port", type=int, default=18082)
    parser.add_argument("--postgres-port", type=int, default=55450)
    args = parser.parse_args()
    if not IDENTIFIER.fullmatch(args.instance):
        raise SystemExit("--instance must be a lowercase deployment identifier")
    if len({args.gateway_port, args.execution_port, args.postgres_port}) != 3 or not all(
        1024 <= value <= 65535
        for value in (args.gateway_port, args.execution_port, args.postgres_port)
    ):
        raise SystemExit("Deployment ports must be distinct and between 1024 and 65535")

    release_dir = args.release_dir.resolve()
    lock_path = release_dir / "release-lock.json"
    if not lock_path.is_file():
        raise SystemExit("P8 release-lock.json is missing; run scripts/build_p8_release.sh")
    raw_lock = json.loads(lock_path.read_text(encoding="utf-8"))
    release = load_release_lock(lock_path, raw_lock.get("release_digest", ""))
    if (
        "app" not in release.get("images", {})
        or release.get("images", {}).get("postgres") != POSTGRES_IMAGE
    ):
        raise SystemExit("Release lock is not a supported P8 deployment lock")
    app_image = args.app_image or release.get("image_refs", {}).get("app")
    if not app_image:
        app_image = "groundwater-v7-app:" + release["version"]
    try:
        inspected_image = subprocess.run(
            ["docker", "image", "inspect", app_image, "--format", "{{.Id}}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"Unable to inspect P8 application image: {app_image}") from error
    if inspected_image != release["images"]["app"]:
        raise SystemExit("Application image differs from the P8 release lock")
    for name in (
        "operator-catalog.json",
        "checker-catalog.json",
        "tool-catalog.json",
        "model-catalog.json",
        "agent-runtime-profile.json",
    ):
        if not (release_dir / name).is_file():
            raise SystemExit(f"Release configuration is incomplete: {name}")
    runtime_profile = json.loads(
        (release_dir / "agent-runtime-profile.json").read_text(encoding="utf-8")
    )
    if (
        "embedding_catalog_digest" in runtime_profile
        and not (release_dir / "embedding-catalog.json").is_file()
    ):
        raise SystemExit("Release configuration is incomplete: embedding-catalog.json")

    runtime = (ROOT / ".p8-runtime" / "instances" / args.instance).resolve()
    env_path = runtime / "p8.env"
    if env_path.exists():
        raise SystemExit(f"Refusing to overwrite {env_path}")
    directories = {
        "V7_P8_POSTGRES_DIR": runtime / "postgres",
        "V7_P8_DATA_DIR": runtime / "data",
        "V7_P8_KNOWLEDGE_DIR": runtime / "knowledge",
        "V7_P8_EXECUTION_DIR": runtime / "execution",
        "V7_P8_SPATIAL_DIR": runtime / "spatial",
        "V7_P8_CONTROL_DIR": runtime / "control",
        "V7_P8_BACKUP_DIR": runtime / "backups",
    }
    runtime.mkdir(parents=True, mode=0o700)
    for path in directories.values():
        path.mkdir(mode=0o700)

    secrets_by_name = {
        name: secrets.token_hex(32)
        for name in (
            "V7_P8_POSTGRES_PASSWORD",
            "V7_PLATFORM_DB_PASSWORD",
            "V7_DATA_DB_PASSWORD",
            "V7_EXECUTION_DB_PASSWORD",
            "V7_KNOWLEDGE_DB_PASSWORD",
            "V7_PLATFORM_DATA_TOKEN",
            "V7_PLATFORM_EXECUTION_TOKEN",
            "V7_PLATFORM_KNOWLEDGE_TOKEN",
            "V7_EXECUTION_DATA_TOKEN",
            "V7_EXECUTION_PLATFORM_TOKEN",
            "V7_KNOWLEDGE_DATA_TOKEN",
        )
    }
    db = {
        service: (
            f"postgresql+psycopg://v7_{service}:{secrets_by_name[f'V7_{service.upper()}_DB_PASSWORD']}"
            f"@postgres:5432/v7_{service}"
        )
        for service in ("platform", "data", "execution", "knowledge")
    }
    images = release["images"]
    config = {
        "V7_P8_PROJECT_NAME": f"groundwater-v7-p8-{args.instance}",
        "V7_P8_ENV_FILE": str(env_path),
        "V7_P8_RELEASE_DIR": str(release_dir),
        "V7_P8_APP_IMAGE": app_image,
        "V7_DEPLOYMENT_ID": args.instance,
        "V7_DOCKER_GID": str(Path("/var/run/docker.sock").stat().st_gid),
        "V7_P8_GATEWAY_BIND": args.gateway_bind,
        "V7_P8_GATEWAY_PORT": str(args.gateway_port),
        "V7_P8_EXECUTION_PORT": str(args.execution_port),
        "V7_P8_POSTGRES_PORT": str(args.postgres_port),
        **{key: str(value) for key, value in directories.items()},
        **secrets_by_name,
        "V7_DATABASE_URL": db["platform"],
        "V7_AGENT_CHECKPOINT_DATABASE_URL": db["platform"],
        "V7_DATA_DATABASE_URL": db["data"],
        "V7_EXECUTION_DATABASE_URL": db["execution"],
        "V7_KNOWLEDGE_DATABASE_URL": db["knowledge"],
        "V7_PLATFORM_URL": "http://platform-api:8007",
        "V7_PLATFORM_UPSTREAM": "http://platform-api:8007",
        "V7_DATA_URL": "http://data-api:8011",
        "V7_EXECUTION_URL": "http://execution-api:8012",
        "V7_KNOWLEDGE_URL": "http://knowledge-api:8013",
        "V7_DATA_HOST": "0.0.0.0",
        "V7_DATA_PORT": "8011",
        "V7_EXECUTION_HOST": "0.0.0.0",
        "V7_EXECUTION_PORT": "8012",
        "V7_EXECUTION_CALLBACK_URL": f"http://127.0.0.1:{args.execution_port}",
        "V7_KNOWLEDGE_HOST": "0.0.0.0",
        "V7_KNOWLEDGE_PORT": "8013",
        "V7_DATA_STORAGE": str(directories["V7_P8_DATA_DIR"]),
        "V7_KNOWLEDGE_STORAGE": str(directories["V7_P8_KNOWLEDGE_DIR"]),
        "V7_EXECUTION_WORKDIR": str(directories["V7_P8_EXECUTION_DIR"]),
        "V7_DATA_SPATIAL_WORK_ROOT": str(directories["V7_P8_SPATIAL_DIR"]),
        "V7_MAINTENANCE_FILE": str(directories["V7_P8_CONTROL_DIR"] / "maintenance"),
        "V7_PARK_MANIFEST": str(directories["V7_P8_CONTROL_DIR"] / "park-manifest.json"),
        "V7_RELEASE_LOCK": str(lock_path),
        "V7_RELEASE_DIGEST": release["release_digest"],
        "V7_OPERATOR_CATALOG": str(release_dir / "operator-catalog.json"),
        "V7_CHECKER_CATALOG": str(release_dir / "checker-catalog.json"),
        "V7_AGENT_TOOL_CATALOG": str(release_dir / "tool-catalog.json"),
        "V7_MODEL_CATALOG": str(release_dir / "model-catalog.json"),
        **(
            {"V7_EMBEDDING_CATALOG": str(release_dir / "embedding-catalog.json")}
            if (release_dir / "embedding-catalog.json").is_file()
            else {}
        ),
        "V7_AGENT_RUNTIME_PROFILE": str(release_dir / "agent-runtime-profile.json"),
        "V7_DEPLOYMENT_MODE": "production",
        "V7_OPERATOR_IMAGE": images["operator"],
        "V7_CHECKER_IMAGE": images["checker"],
        "V7_GDAL_CHECKER_IMAGE": images["gdal_checker"],
        "V7_SPATIAL_QGIS_IMAGE_ID": images["qgis"],
        "V7_SPATIAL_NATIVE_IMAGE_ID": images["spatial_native"],
        "V7_TABULAR_OPERATOR_IMAGE": images["tabular"],
        "V7_CWL_IMAGE": images["cwl"],
        "DEEPSEEK_API_KEY": os.environ.get("DEEPSEEK_API_KEY", "disabled_" + secrets.token_hex(24)),
    }
    fd = os.open(env_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        for key, value in config.items():
            if "\n" in value or "\r" in value:
                raise ValueError("Configuration values cannot contain newlines")
            stream.write(f"{key}={value}\n")
    print(env_path)


if __name__ == "__main__":
    main()
