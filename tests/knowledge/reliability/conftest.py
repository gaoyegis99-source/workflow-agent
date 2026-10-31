import json
import subprocess

# Direct Python invocations must use the same project Docker boundary as bin/ops.
import sys as _maintenance_sys  # noqa: E402
import uuid
from pathlib import Path as _MaintenancePath  # noqa: E402

import pytest
from platform_core.application.checker_catalog import build_catalog

from scripts.lib.runtime_layout import P5_RUNTIME_ROOT
from tests.platform.database.conftest import database, owner, postgres  # noqa: F401
from tests.platform.services.conftest import service_databases  # noqa: F401
from tests.platform.services.harness import Cluster

_maintenance_source = next(
    parent
    for parent in _MaintenancePath(__file__).resolve().parents
    if (parent / "scripts/maintenance/project_environment.py").is_file()
)
_maintenance_sys.path.insert(0, str(_maintenance_source))
from scripts.maintenance.project_environment import docker_executable  # noqa: E402


@pytest.fixture
def r2_cluster(tmp_path, database, service_databases):  # noqa: F811
    cluster = Cluster(tmp_path, database, service_databases)
    lock_path = (P5_RUNTIME_ROOT / "checker-catalog.json").resolve()
    cluster.configure_checker_catalog(lock_path)
    try:
        cluster.start_services()
        yield cluster
    finally:
        cluster.close()


@pytest.fixture(scope="session")
def delayed_checker_image(tmp_path_factory):
    """Build a test-only checker image with a deterministic termination window."""

    lock = json.loads((P5_RUNTIME_ROOT / "checker-catalog.json").read_text())
    root = tmp_path_factory.mktemp("p5-r21-delayed-checker")
    suffix = uuid.uuid4().hex
    base_tag = "groundwater-v7-checker-r21-base:" + suffix
    tag = "groundwater-v7-checker-r21-test:" + suffix
    subprocess.run(
        [docker_executable(), "tag", lock["images"]["CHECKER"], base_tag],
        check=True,
        capture_output=True,
    )
    dockerfile = root / "Dockerfile"
    dockerfile.write_text(
        f'FROM {base_tag}\nENTRYPOINT ["sh", "-c", "sleep 8; exec platform-checker"]\n',
        encoding="utf-8",
    )
    subprocess.run(
        [docker_executable(), "build", "-t", tag, str(root)],
        check=True,
        capture_output=True,
    )
    image = subprocess.run(
        [docker_executable(), "image", "inspect", tag, "--format", "{{.Id}}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    try:
        yield image
    finally:
        subprocess.run(
            [docker_executable(), "image", "rm", "-f", tag, base_tag],
            check=False,
            capture_output=True,
        )


@pytest.fixture
def delayed_r2_cluster(tmp_path, database, service_databases, delayed_checker_image):  # noqa: F811
    cluster = Cluster(tmp_path, database, service_databases)
    base = json.loads((P5_RUNTIME_ROOT / "checker-catalog.json").read_text())
    images = {
        "CHECKER": delayed_checker_image,
        "GDAL_CHECKER": base["images"]["GDAL_CHECKER"],
    }
    lock_path = tmp_path / "checker-catalog.json"
    lock_path.write_text(
        json.dumps(
            {"schema_version": 1, "images": images, "catalog": build_catalog(images)},
            indent=2,
        ),
        encoding="utf-8",
    )
    cluster.configure_checker_catalog(lock_path)
    try:
        cluster.start_services()
        yield cluster
    finally:
        cluster.close()
