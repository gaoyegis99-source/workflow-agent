#!/usr/bin/env python3
"""Create a non-overwriting, direct-Compose V7 deployment configuration.

The configurator accepts explicit artifacts and image identities, verifies only
their local shape and image availability, then writes a non-secret Compose
control file plus narrowly scoped 0600 service environments.  It does not read
a Release Lock, mutate an existing instance, start Docker, or alter the daemon.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import pwd
import re
import secrets
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = ROOT.parent
if __package__ in {None, ""}:
    sys.path.insert(0, str(ROOT))

from scripts.lib.runtime_layout import P8_RUNTIME_ROOT  # noqa: E402
from scripts.maintenance.project_environment import (  # noqa: E402
    EnvironmentRefusal,
    docker_executable,
    require_socket,
)

IDENTIFIER = re.compile(r"[a-z][a-z0-9-]{0,31}")
IMAGE_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
ARTIFACT_FILES = (
    "operator-catalog.json",
    "checker-catalog.json",
    "tool-catalog.json",
    "model-catalog.json",
    "agent-runtime-profile.json",
)


def ipv4_address(value: str) -> str:
    try:
        return str(ipaddress.IPv4Address(value))
    except ipaddress.AddressValueError as error:
        raise argparse.ArgumentTypeError("must be an IPv4 address") from error


def _owned_directory(path: Path, *, name: str) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"{name}_INVALID")
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(PROJECT_ROOT):
        raise ValueError(f"{name}_OUTSIDE_PROJECT")
    return resolved


def _owned_new_directory(path: Path, *, name: str) -> Path:
    if path.exists() or path.is_symlink():
        raise ValueError(f"{name}_ALREADY_EXISTS")
    resolved = path.resolve()
    if not resolved.is_relative_to(PROJECT_ROOT):
        raise ValueError(f"{name}_OUTSIDE_PROJECT")
    return resolved


def _artifact_directory(path: Path) -> tuple[Path, dict[str, str]]:
    artifact = _owned_directory(path, name="ARTIFACT_DIRECTORY")
    digests: dict[str, str] = {}
    for name in ARTIFACT_FILES:
        candidate = artifact / name
        if candidate.is_symlink() or not candidate.is_file():
            raise ValueError(f"ARTIFACT_FILE_REQUIRED:{name}")
        digests[name] = hashlib.sha256(candidate.read_bytes()).hexdigest()
    try:
        runtime_profile = json.loads(
            (artifact / "agent-runtime-profile.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("ARTIFACT_RUNTIME_PROFILE_INVALID") from error
    if not isinstance(runtime_profile, dict):
        raise ValueError("ARTIFACT_RUNTIME_PROFILE_INVALID")
    embedding = artifact / "embedding-catalog.json"
    if "embedding_catalog_digest" in runtime_profile:
        if embedding.is_symlink() or not embedding.is_file():
            raise ValueError("ARTIFACT_FILE_REQUIRED:embedding-catalog.json")
        digests[embedding.name] = hashlib.sha256(embedding.read_bytes()).hexdigest()
    return artifact, digests


def _deployment_revision(digests: Mapping[str, str]) -> str:
    payload = "\n".join(f"{name}:{digest}" for name, digest in sorted(digests.items()))
    return "sha256:" + hashlib.sha256(payload.encode("ascii")).hexdigest()


def _inspect_image(image: str) -> None:
    if not image or "\n" in image or "\r" in image:
        raise ValueError("APPLICATION_IMAGE_INVALID")
    try:
        completed = subprocess.run(
            [docker_executable(), "image", "inspect", image, "--format", "{{.Id}}"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as error:
        raise ValueError("APPLICATION_IMAGE_UNAVAILABLE") from error
    if not completed.stdout.strip():
        raise ValueError("APPLICATION_IMAGE_UNAVAILABLE")


def _container_group_id(
    host_gid: int,
    *,
    username: str | None = None,
    primary_gid: int | None = None,
    subordinate_file: Path = Path("/etc/subgid"),
) -> int:
    """Translate a rootless daemon's host GID into its container GID."""
    primary = os.getgid() if primary_gid is None else primary_gid
    if host_gid == primary:
        return 0
    if username is None:
        try:
            username = pwd.getpwuid(os.getuid()).pw_name
        except KeyError as error:
            raise ValueError("DOCKER_SOCKET_GID_UNMAPPED") from error
    try:
        entries = subordinate_file.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ValueError("DOCKER_SOCKET_GID_UNMAPPED") from error
    for entry in entries:
        fields = entry.split(":")
        if len(fields) != 3 or fields[0] != username:
            continue
        try:
            start, count = int(fields[1]), int(fields[2])
        except ValueError:
            continue
        if start <= host_gid < start + count:
            return host_gid - start + 1
    raise ValueError("DOCKER_SOCKET_GID_UNMAPPED")


def _science_image(value: str, name: str) -> str:
    if not IMAGE_DIGEST.fullmatch(value or ""):
        raise ValueError(f"{name}_INVALID")
    # A syntactically valid digest is not sufficient: an instance that refers
    # to a missing science image cannot pass readiness or recover a workload.
    # The project launcher performs this read-only check against only V7's
    # rootless daemon and refuses to fall back to the host daemon.
    _inspect_image(value)
    return value


def _write_env(path: Path, values: Mapping[str, str]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        for key, value in sorted(values.items()):
            if not key.startswith("V7_") and key not in {"POSTGRES_PASSWORD", "DEEPSEEK_API_KEY"}:
                raise ValueError("ENVIRONMENT_KEY_INVALID")
            if not isinstance(value, str) or "\n" in value or "\r" in value:
                raise ValueError("ENVIRONMENT_VALUE_INVALID")
            stream.write(f"{key}={value}\n")


def _token() -> str:
    return secrets.token_hex(32)


def _common_runtime_values(
    *,
    artifact: Path,
    directories: Mapping[str, Path],
    deployment_id: str,
    deployment_revision: str,
    images: Mapping[str, str],
    identity_mode: str,
) -> dict[str, str]:
    passwords = {name: _token() for name in ("platform", "data", "execution", "knowledge")}
    database = {
        service: f"postgresql+psycopg://v7_{service}:{passwords[service]}@postgres:5432/v7_{service}"
        for service in passwords
    }
    values = {
        "V7_DEPLOYMENT_MODE": "direct",
        "V7_DEPLOYMENT_ID": deployment_id,
        "V7_DEPLOYMENT_REVISION": deployment_revision,
        "V7_IDENTITY_MODE": identity_mode,
        "V7_DATABASE_URL": database["platform"],
        "V7_AGENT_CHECKPOINT_DATABASE_URL": database["platform"],
        "V7_DATA_DATABASE_URL": database["data"],
        "V7_EXECUTION_DATABASE_URL": database["execution"],
        "V7_KNOWLEDGE_DATABASE_URL": database["knowledge"],
        "V7_PLATFORM_URL": "http://platform-api:8007",
        "V7_PLATFORM_UPSTREAM": "http://platform-api:8007",
        "V7_DATA_URL": "http://data-api:8011",
        "V7_EXECUTION_URL": "http://execution-api:8012",
        "V7_KNOWLEDGE_URL": "http://knowledge-api:8013",
        "V7_DATA_HOST": "0.0.0.0",
        "V7_DATA_PORT": "8011",
        "V7_EXECUTION_HOST": "0.0.0.0",
        "V7_EXECUTION_PORT": "8012",
        "V7_EXECUTION_CALLBACK_URL": "http://execution-api:8012",
        "V7_EXECUTION_WORKLOAD_NETWORK": f"groundwater-v7-p8-{deployment_id}-v7-workload",
        "V7_KNOWLEDGE_HOST": "0.0.0.0",
        "V7_KNOWLEDGE_PORT": "8013",
        "V7_DATA_STORAGE": str(directories["data"]),
        "V7_KNOWLEDGE_STORAGE": str(directories["knowledge"]),
        "V7_EXECUTION_WORKDIR": str(directories["execution"]),
        "V7_DATA_SPATIAL_WORK_ROOT": str(directories["spatial"]),
        "V7_MAINTENANCE_FILE": str(directories["control"] / "maintenance"),
        "V7_PARK_MANIFEST": str(directories["control"] / "park-manifest.json"),
        "V7_OPERATOR_CATALOG": str(artifact / "operator-catalog.json"),
        "V7_CHECKER_CATALOG": str(artifact / "checker-catalog.json"),
        "V7_AGENT_TOOL_CATALOG": str(artifact / "tool-catalog.json"),
        "V7_MODEL_CATALOG": str(artifact / "model-catalog.json"),
        "V7_AGENT_RUNTIME_PROFILE": str(artifact / "agent-runtime-profile.json"),
        "V7_OPERATOR_IMAGE": images["operator"],
        "V7_CHECKER_IMAGE": images["checker"],
        "V7_GDAL_CHECKER_IMAGE": images["gdal_checker"],
        "V7_SPATIAL_QGIS_IMAGE_ID": images["qgis"],
        "V7_SPATIAL_NATIVE_IMAGE_ID": images["spatial_native"],
        "V7_TABULAR_OPERATOR_IMAGE": images["tabular"],
        "V7_CWL_IMAGE": images["cwl"],
        "V7_P8_POSTGRES_PASSWORD": _token(),
        "V7_PLATFORM_DB_PASSWORD": passwords["platform"],
        "V7_DATA_DB_PASSWORD": passwords["data"],
        "V7_EXECUTION_DB_PASSWORD": passwords["execution"],
        "V7_KNOWLEDGE_DB_PASSWORD": passwords["knowledge"],
        "V7_PLATFORM_DATA_TOKEN": _token(),
        "V7_PLATFORM_EXECUTION_TOKEN": _token(),
        "V7_PLATFORM_KNOWLEDGE_TOKEN": _token(),
        "V7_EXECUTION_DATA_TOKEN": _token(),
        "V7_EXECUTION_PLATFORM_TOKEN": _token(),
        "V7_KNOWLEDGE_DATA_TOKEN": _token(),
        "DEEPSEEK_API_KEY": os.environ.get("DEEPSEEK_API_KEY", "disabled_" + _token()),
    }
    embedding = artifact / "embedding-catalog.json"
    if embedding.is_file() and not embedding.is_symlink():
        values["V7_EMBEDDING_CATALOG"] = str(embedding)
    return values


def _service_environments(
    values: Mapping[str, str], *, proxy_token: str
) -> dict[str, dict[str, str]]:
    # Compose control state intentionally contains no secret.  This function is
    # the one place where every process's least-privilege environment is
    # declared, so a new service cannot accidentally inherit the old p8.env.
    deployment = {
        key: values[key]
        for key in ("V7_DEPLOYMENT_MODE", "V7_DEPLOYMENT_ID", "V7_DEPLOYMENT_REVISION")
    }
    catalog_paths = {
        key: values[key]
        for key in (
            "V7_OPERATOR_CATALOG",
            "V7_CHECKER_CATALOG",
            "V7_AGENT_TOOL_CATALOG",
            "V7_MODEL_CATALOG",
            "V7_AGENT_RUNTIME_PROFILE",
        )
    }
    execution_images = {
        key: values[key]
        for key in (
            "V7_OPERATOR_IMAGE",
            "V7_CHECKER_IMAGE",
            "V7_GDAL_CHECKER_IMAGE",
            "V7_SPATIAL_QGIS_IMAGE_ID",
            "V7_SPATIAL_NATIVE_IMAGE_ID",
            "V7_TABULAR_OPERATOR_IMAGE",
            "V7_CWL_IMAGE",
        )
    }
    endpoints = {
        key: values[key]
        for key in (
            "V7_PLATFORM_URL",
            "V7_DATA_URL",
            "V7_EXECUTION_URL",
            "V7_KNOWLEDGE_URL",
            "V7_EXECUTION_CALLBACK_URL",
            "V7_EXECUTION_WORKLOAD_NETWORK",
        )
    }
    database = {
        key: values[key]
        for key in (
            "V7_DATABASE_URL",
            "V7_AGENT_CHECKPOINT_DATABASE_URL",
            "V7_DATA_DATABASE_URL",
            "V7_EXECUTION_DATABASE_URL",
            "V7_KNOWLEDGE_DATABASE_URL",
        )
    }
    tokens = {
        key: values[key]
        for key in (
            "V7_PLATFORM_DATA_TOKEN",
            "V7_PLATFORM_EXECUTION_TOKEN",
            "V7_PLATFORM_KNOWLEDGE_TOKEN",
            "V7_EXECUTION_DATA_TOKEN",
            "V7_EXECUTION_PLATFORM_TOKEN",
            "V7_KNOWLEDGE_DATA_TOKEN",
        )
    }
    platform = (
        deployment
        | catalog_paths
        | execution_images
        | {
            "V7_DATABASE_URL": database["V7_DATABASE_URL"],
            "V7_DATA_URL": endpoints["V7_DATA_URL"],
            "V7_EXECUTION_URL": endpoints["V7_EXECUTION_URL"],
            "V7_KNOWLEDGE_URL": endpoints["V7_KNOWLEDGE_URL"],
            "V7_PLATFORM_DATA_TOKEN": tokens["V7_PLATFORM_DATA_TOKEN"],
            "V7_PLATFORM_EXECUTION_TOKEN": tokens["V7_PLATFORM_EXECUTION_TOKEN"],
            "V7_PLATFORM_KNOWLEDGE_TOKEN": tokens["V7_PLATFORM_KNOWLEDGE_TOKEN"],
            "V7_EXECUTION_PLATFORM_TOKEN": tokens["V7_EXECUTION_PLATFORM_TOKEN"],
            "V7_IDENTITY_MODE": values["V7_IDENTITY_MODE"],
            "V7_MAINTENANCE_FILE": values["V7_MAINTENANCE_FILE"],
        }
    )
    gateway = {
        **deployment,
        "V7_PLATFORM_UPSTREAM": values["V7_PLATFORM_UPSTREAM"],
        "V7_MAINTENANCE_FILE": values["V7_MAINTENANCE_FILE"],
        "V7_IDENTITY_MODE": values["V7_IDENTITY_MODE"],
    }
    if proxy_token:
        gateway["V7_TRUSTED_PROXY_TOKEN"] = proxy_token
    execution = deployment | {
        "V7_OPERATOR_CATALOG": catalog_paths["V7_OPERATOR_CATALOG"],
        "V7_CHECKER_CATALOG": catalog_paths["V7_CHECKER_CATALOG"],
        **execution_images,
        "V7_EXECUTION_DATABASE_URL": database["V7_EXECUTION_DATABASE_URL"],
        "V7_PLATFORM_URL": endpoints["V7_PLATFORM_URL"],
        "V7_DATA_URL": endpoints["V7_DATA_URL"],
        "V7_EXECUTION_CALLBACK_URL": endpoints["V7_EXECUTION_CALLBACK_URL"],
        "V7_EXECUTION_WORKLOAD_NETWORK": endpoints["V7_EXECUTION_WORKLOAD_NETWORK"],
        "V7_EXECUTION_HOST": values["V7_EXECUTION_HOST"],
        "V7_EXECUTION_PORT": values["V7_EXECUTION_PORT"],
        "V7_PLATFORM_EXECUTION_TOKEN": tokens["V7_PLATFORM_EXECUTION_TOKEN"],
        "V7_EXECUTION_DATA_TOKEN": tokens["V7_EXECUTION_DATA_TOKEN"],
        "V7_EXECUTION_PLATFORM_TOKEN": tokens["V7_EXECUTION_PLATFORM_TOKEN"],
        "V7_EXECUTION_WORKDIR": values["V7_EXECUTION_WORKDIR"],
    }
    data_common = deployment | {
        "V7_DATA_DATABASE_URL": database["V7_DATA_DATABASE_URL"],
        "V7_DATA_STORAGE": values["V7_DATA_STORAGE"],
    }
    data_api = data_common | {
        "V7_DATA_HOST": values["V7_DATA_HOST"],
        "V7_DATA_PORT": values["V7_DATA_PORT"],
        "V7_PLATFORM_DATA_TOKEN": tokens["V7_PLATFORM_DATA_TOKEN"],
        "V7_EXECUTION_DATA_TOKEN": tokens["V7_EXECUTION_DATA_TOKEN"],
        "V7_KNOWLEDGE_DATA_TOKEN": tokens["V7_KNOWLEDGE_DATA_TOKEN"],
    }
    data_spatial = data_common | {
        "V7_DATA_SPATIAL_WORK_ROOT": values["V7_DATA_SPATIAL_WORK_ROOT"],
        "V7_SPATIAL_QGIS_IMAGE_ID": execution_images["V7_SPATIAL_QGIS_IMAGE_ID"],
    }
    knowledge = deployment | {
        "V7_KNOWLEDGE_DATABASE_URL": database["V7_KNOWLEDGE_DATABASE_URL"],
        "V7_KNOWLEDGE_STORAGE": values["V7_KNOWLEDGE_STORAGE"],
        "V7_KNOWLEDGE_HOST": values["V7_KNOWLEDGE_HOST"],
        "V7_KNOWLEDGE_PORT": values["V7_KNOWLEDGE_PORT"],
        "V7_PLATFORM_KNOWLEDGE_TOKEN": tokens["V7_PLATFORM_KNOWLEDGE_TOKEN"],
        "V7_DATA_URL": values["V7_DATA_URL"],
        "V7_KNOWLEDGE_DATA_TOKEN": tokens["V7_KNOWLEDGE_DATA_TOKEN"],
    }
    package = deployment | {
        "V7_DATABASE_URL": database["V7_DATABASE_URL"],
        "V7_OPERATOR_CATALOG": catalog_paths["V7_OPERATOR_CATALOG"],
        "V7_CWL_IMAGE": execution_images["V7_CWL_IMAGE"],
    }
    action = deployment | {
        "V7_DATABASE_URL": database["V7_DATABASE_URL"],
        "V7_EXECUTION_URL": endpoints["V7_EXECUTION_URL"],
        "V7_PLATFORM_EXECUTION_TOKEN": tokens["V7_PLATFORM_EXECUTION_TOKEN"],
    }
    acceptance = dict(action)
    lifecycle = deployment | {
        "V7_DATABASE_URL": database["V7_DATABASE_URL"],
        "V7_KNOWLEDGE_URL": endpoints["V7_KNOWLEDGE_URL"],
        "V7_PLATFORM_KNOWLEDGE_TOKEN": tokens["V7_PLATFORM_KNOWLEDGE_TOKEN"],
    }
    authoring = deployment | {
        "V7_DATABASE_URL": database["V7_DATABASE_URL"],
        "V7_OPERATOR_CATALOG": catalog_paths["V7_OPERATOR_CATALOG"],
        "V7_CWL_IMAGE": execution_images["V7_CWL_IMAGE"],
        "V7_DATA_URL": endpoints["V7_DATA_URL"],
        "V7_EXECUTION_URL": endpoints["V7_EXECUTION_URL"],
        "V7_PLATFORM_DATA_TOKEN": tokens["V7_PLATFORM_DATA_TOKEN"],
        "V7_PLATFORM_EXECUTION_TOKEN": tokens["V7_PLATFORM_EXECUTION_TOKEN"],
    }
    agent = (
        deployment
        | catalog_paths
        | {
            "V7_DATABASE_URL": database["V7_DATABASE_URL"],
            "V7_AGENT_CHECKPOINT_DATABASE_URL": database["V7_AGENT_CHECKPOINT_DATABASE_URL"],
            "V7_CHECKER_IMAGE": execution_images["V7_CHECKER_IMAGE"],
            "V7_GDAL_CHECKER_IMAGE": execution_images["V7_GDAL_CHECKER_IMAGE"],
            "V7_DATA_URL": endpoints["V7_DATA_URL"],
            "V7_KNOWLEDGE_URL": endpoints["V7_KNOWLEDGE_URL"],
            "V7_EXECUTION_URL": endpoints["V7_EXECUTION_URL"],
            "V7_PLATFORM_DATA_TOKEN": tokens["V7_PLATFORM_DATA_TOKEN"],
            "V7_PLATFORM_KNOWLEDGE_TOKEN": tokens["V7_PLATFORM_KNOWLEDGE_TOKEN"],
            "V7_PLATFORM_EXECUTION_TOKEN": tokens["V7_PLATFORM_EXECUTION_TOKEN"],
            "DEEPSEEK_API_KEY": values["DEEPSEEK_API_KEY"],
        }
    )
    if "V7_EMBEDDING_CATALOG" in values:
        agent["V7_EMBEDDING_CATALOG"] = values["V7_EMBEDDING_CATALOG"]
    backup = (
        deployment
        | catalog_paths
        | {
            "V7_DATABASE_URL": database["V7_DATABASE_URL"],
            "V7_DATA_DATABASE_URL": database["V7_DATA_DATABASE_URL"],
            "V7_EXECUTION_DATABASE_URL": database["V7_EXECUTION_DATABASE_URL"],
            "V7_KNOWLEDGE_DATABASE_URL": database["V7_KNOWLEDGE_DATABASE_URL"],
            "V7_EXECUTION_WORKDIR": values["V7_EXECUTION_WORKDIR"],
            "V7_DATA_STORAGE": values["V7_DATA_STORAGE"],
            "V7_KNOWLEDGE_STORAGE": values["V7_KNOWLEDGE_STORAGE"],
            "V7_DATA_SPATIAL_WORK_ROOT": values["V7_DATA_SPATIAL_WORK_ROOT"],
            "V7_MAINTENANCE_FILE": values["V7_MAINTENANCE_FILE"],
        }
    )
    migration = database | deployment
    postgres = {
        "POSTGRES_PASSWORD": values["V7_P8_POSTGRES_PASSWORD"],
        "V7_PLATFORM_DB_PASSWORD": values["V7_PLATFORM_DB_PASSWORD"],
        "V7_DATA_DB_PASSWORD": values["V7_DATA_DB_PASSWORD"],
        "V7_EXECUTION_DB_PASSWORD": values["V7_EXECUTION_DB_PASSWORD"],
        "V7_KNOWLEDGE_DB_PASSWORD": values["V7_KNOWLEDGE_DB_PASSWORD"],
    }
    result = {
        "postgres": postgres,
        "migrate": migration,
        "platform-api": platform,
        "data-api": data_api,
        "execution-api": execution,
        "knowledge-api": knowledge,
        "gateway": gateway,
        "platform-worker-package": package,
        "platform-worker-action": action,
        "platform-worker-acceptance": acceptance,
        "platform-worker-knowledge": lifecycle,
        "authoring-worker": authoring,
        "data-worker": data_common,
        "data-document-worker": data_common,
        "data-spatial-worker": data_spatial,
        "execution-supervisor": execution,
        "execution-node-1": execution,
        "execution-node-2": execution,
        "execution-publication": execution,
        "knowledge-worker": knowledge,
        "agent-worker": agent,
        "backup": backup,
    }
    return {name: dict(value) for name, value in result.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance", default="local")
    parser.add_argument("--runtime-root", type=Path, default=P8_RUNTIME_ROOT)
    parser.add_argument("--artifact-dir", type=Path, required=True)
    parser.add_argument("--app-image", required=True)
    parser.add_argument("--operator-image", required=True)
    parser.add_argument("--checker-image", required=True)
    parser.add_argument("--gdal-checker-image", required=True)
    parser.add_argument("--spatial-qgis-image", required=True)
    parser.add_argument("--spatial-native-image", required=True)
    parser.add_argument("--tabular-image", required=True)
    parser.add_argument("--cwl-image", required=True)
    parser.add_argument("--gateway-bind", type=ipv4_address, default="127.0.0.1")
    parser.add_argument("--gateway-port", type=int, default=18080)
    parser.add_argument("--identity-mode", choices=("token", "trusted-proxy"), default="token")
    parser.add_argument("--trusted-proxy-token-file", type=Path)
    args = parser.parse_args()

    if not IDENTIFIER.fullmatch(args.instance):
        raise SystemExit("--instance must be a lowercase deployment identifier")
    if not 1024 <= args.gateway_port <= 65535:
        raise SystemExit("--gateway-port must be between 1024 and 65535")
    if args.trusted_proxy_token_file and args.identity_mode != "trusted-proxy":
        raise SystemExit("--trusted-proxy-token-file requires --identity-mode trusted-proxy")
    if args.identity_mode == "trusted-proxy" and not args.trusted_proxy_token_file:
        raise SystemExit("trusted-proxy mode requires --trusted-proxy-token-file")
    try:
        artifact, digests = _artifact_directory(args.artifact_dir)
        _inspect_image(args.app_image)
        docker_socket = require_socket()
        docker_gid = _container_group_id(docker_socket.stat().st_gid)
    except (EnvironmentRefusal, ValueError) as error:
        raise SystemExit(str(error)) from error

    images = {
        "operator": _science_image(args.operator_image, "V7_OPERATOR_IMAGE"),
        "checker": _science_image(args.checker_image, "V7_CHECKER_IMAGE"),
        "gdal_checker": _science_image(args.gdal_checker_image, "V7_GDAL_CHECKER_IMAGE"),
        "qgis": _science_image(args.spatial_qgis_image, "V7_SPATIAL_QGIS_IMAGE"),
        "spatial_native": _science_image(args.spatial_native_image, "V7_SPATIAL_NATIVE_IMAGE"),
        "tabular": _science_image(args.tabular_image, "V7_TABULAR_OPERATOR_IMAGE"),
        "cwl": _science_image(args.cwl_image, "V7_CWL_IMAGE"),
    }
    if args.identity_mode == "trusted-proxy":
        assert args.trusted_proxy_token_file is not None
        token_file = args.trusted_proxy_token_file.expanduser()
        if token_file.is_symlink() or not token_file.is_file() or token_file.stat().st_mode & 0o077:
            raise SystemExit("--trusted-proxy-token-file must be a non-symlink 0600 file")
        proxy_token = token_file.read_text(encoding="utf-8").strip()
        if len(proxy_token) < 32:
            raise SystemExit("Trusted-proxy token must contain at least 32 characters")
    else:
        proxy_token = ""

    try:
        runtime_root = _owned_directory(args.runtime_root, name="RUNTIME_ROOT")
        runtime = _owned_new_directory(runtime_root / "instances" / args.instance, name="INSTANCE")
    except ValueError as error:
        raise SystemExit(str(error)) from error
    runtime.mkdir(parents=True, mode=0o700)
    directories = {
        name: runtime / name
        for name in ("postgres", "data", "knowledge", "execution", "spatial", "control", "backups")
    }
    for directory in directories.values():
        directory.mkdir(mode=0o700)
    environment_dir = runtime / "environment"
    environment_dir.mkdir(mode=0o700)

    values = _common_runtime_values(
        artifact=artifact,
        directories=directories,
        deployment_id=args.instance,
        deployment_revision=_deployment_revision(digests),
        images=images,
        identity_mode=args.identity_mode,
    )
    services = _service_environments(values, proxy_token=proxy_token)
    try:
        for service, environment in services.items():
            _write_env(environment_dir / f"{service}.env", environment)
        control = {
            "V7_P8_PROJECT_NAME": f"groundwater-v7-p8-{args.instance}",
            "V7_P8_CONTROL_ENV_FILE": str(runtime / "compose.env"),
            "V7_P8_ARTIFACT_DIR": str(artifact),
            "V7_P8_APP_IMAGE": args.app_image,
            "V7_DOCKER_SOCKET": str(docker_socket),
            "V7_DOCKER_GID": str(docker_gid),
            "V7_P8_GATEWAY_BIND": args.gateway_bind,
            "V7_P8_GATEWAY_PORT": str(args.gateway_port),
            "V7_P8_POSTGRES_DIR": str(directories["postgres"]),
            "V7_P8_DATA_DIR": str(directories["data"]),
            "V7_P8_KNOWLEDGE_DIR": str(directories["knowledge"]),
            "V7_P8_EXECUTION_DIR": str(directories["execution"]),
            "V7_P8_SPATIAL_DIR": str(directories["spatial"]),
            "V7_P8_CONTROL_DIR": str(directories["control"]),
            "V7_P8_BACKUP_DIR": str(directories["backups"]),
            **{
                f"V7_P8_{service.upper().replace('-', '_')}_ENV_FILE": str(
                    environment_dir / f"{service}.env"
                )
                for service in services
            },
        }
        _write_env(runtime / "compose.env", control)
    except (FileExistsError, ValueError) as error:
        raise SystemExit(str(error)) from error
    print(runtime / "compose.env")


if __name__ == "__main__":
    main()
