import json
import subprocess
import uuid
from pathlib import Path

import pytest
from platform_core.application.checker_catalog import build_catalog

from tests.p2.conftest import database, owner, postgres  # noqa: F401
from tests.p3.conftest import service_databases  # noqa: F401
from tests.p3.harness import Cluster


@pytest.fixture
def r2_cluster(tmp_path, database, service_databases):  # noqa: F811
    cluster = Cluster(tmp_path, database, service_databases)
    lock_path = Path(".p5-runtime/checker-catalog.json").resolve()
    lock = json.loads(lock_path.read_text())
    cluster.env |= {
        "V7_CHECKER_CATALOG": str(lock_path),
        "V7_CHECKER_IMAGE": lock["images"]["CHECKER"],
        "V7_GDAL_CHECKER_IMAGE": lock["images"]["GDAL_CHECKER"],
    }
    try:
        cluster.start_services()
        yield cluster
    finally:
        cluster.close()


@pytest.fixture(scope="session")
def delayed_checker_image(tmp_path_factory):
    """Build a test-only checker image with a deterministic termination window."""

    lock = json.loads(Path(".p5-runtime/checker-catalog.json").read_text())
    root = tmp_path_factory.mktemp("p5-r21-delayed-checker")
    suffix = uuid.uuid4().hex
    base_tag = "groundwater-v7-checker-r21-base:" + suffix
    tag = "groundwater-v7-checker-r21-test:" + suffix
    subprocess.run(
        ["docker", "tag", lock["images"]["CHECKER"], base_tag],
        check=True,
        capture_output=True,
    )
    dockerfile = root / "Dockerfile"
    dockerfile.write_text(
        f'FROM {base_tag}\nENTRYPOINT ["sh", "-c", "sleep 8; exec platform-checker"]\n',
        encoding="utf-8",
    )
    subprocess.run(
        ["docker", "build", "-t", tag, str(root)],
        check=True,
        capture_output=True,
    )
    image = subprocess.run(
        ["docker", "image", "inspect", tag, "--format", "{{.Id}}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    try:
        yield image
    finally:
        subprocess.run(
            ["docker", "image", "rm", "-f", tag, base_tag], check=False, capture_output=True
        )


@pytest.fixture
def delayed_r2_cluster(tmp_path, database, service_databases, delayed_checker_image):  # noqa: F811
    cluster = Cluster(tmp_path, database, service_databases)
    base = json.loads(Path(".p5-runtime/checker-catalog.json").read_text())
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
    cluster.env |= {
        "V7_CHECKER_CATALOG": str(lock_path),
        "V7_CHECKER_IMAGE": images["CHECKER"],
        "V7_GDAL_CHECKER_IMAGE": images["GDAL_CHECKER"],
    }
    try:
        cluster.start_services()
        yield cluster
    finally:
        cluster.close()
