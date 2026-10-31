import json

import pytest
from fastapi.testclient import TestClient
from platform_data.adapters.blobs import Blobs
from platform_data.api import create_app
from platform_data.worker import Worker

from scripts.lib.runtime_layout import P4_RUNTIME_ROOT
from tests.platform.database.conftest import database, owner, postgres  # noqa: F401
from tests.platform.services.conftest import service_databases  # noqa: F401


@pytest.fixture(scope="session")
def spatial_images():
    value = json.loads((P4_RUNTIME_ROOT / "spatial/images.json").read_text())
    assert set(value["images"]) == {"qgis", "native"}
    return value["images"]


@pytest.fixture
def data_client(service_databases, tmp_path):  # noqa: F811
    data_db, _ = service_databases
    blobs = Blobs(tmp_path / "blobs")
    app = create_app(data_db, blobs, {"platform": "a" * 48, "execution": "b" * 48})
    with TestClient(
        app,
        headers={
            "Authorization": "Bearer " + "a" * 48,
            "X-Actor-ID": "owner",
            "Idempotency-Key": "default",
        },
    ) as client:
        yield client, Worker(data_db, blobs), data_db


@pytest.fixture
def spatial_data(data_client, spatial_images, tmp_path):
    from platform_data.adapters.spatial import SpatialInspector
    from platform_data.spatial_worker import SpatialWorker

    client, table_worker, data_db = data_client
    worker = SpatialWorker(
        data_db,
        table_worker.blobs,
        SpatialInspector(spatial_images["qgis"], tmp_path / "inspection"),
    )
    return client, worker, data_db
