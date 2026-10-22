import io
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from platform_data.adapters import tables
from platform_data.adapters.blobs import Blobs
from platform_data.api import create_app
from platform_data.domain.model import checksum
from platform_data.worker import Worker

CONTENT = b"station,date,level\n001,2024-01-01,1\n001,2024-01-02,2\n"
CONFIG = {
    "columns": [{"name": name, "type": "string"} for name in ["station", "date", "level"]],
    "null_values": [""],
}
P = "/internal/projects/project"


@pytest.fixture
def data_client(service_databases, tmp_path):
    db, _ = service_databases
    blobs = Blobs(tmp_path / "blobs")
    app = create_app(db, blobs, {"platform": "a" * 48, "execution": "b" * 48})
    with TestClient(
        app,
        headers={
            "Authorization": "Bearer " + "a" * 48,
            "X-Actor-ID": "owner",
            "Idempotency-Key": "default",
        },
    ) as client:
        yield client, Worker(db, blobs), db


def uploaded(client, content=CONTENT, format="csv", key="create"):
    response = client.post(
        P + "/uploads",
        headers={"Idempotency-Key": key},
        json={
            "filename": "monitoring." + format,
            "format": format,
            "size": len(content),
            "digest": checksum(content),
        },
    )
    assert response.status_code == 201, response.text
    id = response.json()["upload_id"]
    assert client.put(P + "/uploads/" + id + "/content", content=content).status_code == 200
    return id


def finalized(client, worker, content=CONTENT, format="csv", config=CONFIG):
    id = uploaded(client, content, format)
    response = client.post(f"{P}/uploads/{id}/finalize", json=config)
    assert response.status_code == 202, response.text
    assert client.get(P + "/assets").json()["items"] == []
    assert worker.tick()
    return client.get(f"{P}/uploads/{id}").json()


def test_csv_explicit_types_digest_semantics_and_scope(data_client):
    client, worker, db = data_client
    upload = finalized(client, worker)
    assert upload["status"] == "READY", upload
    ref = upload["asset"]
    url = f"{P}/assets/{ref['asset_id']}/versions/1"
    asset = client.get(url).json()
    assert [f["field_id"] for f in asset["manifest"]["fields"]] == ["f0", "f1", "f2"]
    content = client.get(url + "/representations/normalized/content").content
    assert pq.read_table(pa.BufferReader(content))["f0"].to_pylist() == ["001", "001"]
    semantic = {
        "expected_revision": 0,
        "version_id": "1",
        "semantics": {"timezone": "UTC", "value_unit": "m", "quantity": "water_level_elevation"},
    }
    assert (
        client.post(f"{P}/assets/{ref['asset_id']}/semantic-revisions", json=semantic).status_code
        == 201
    )
    assert (
        client.post(
            f"{P}/assets/{ref['asset_id']}/semantic-revisions",
            json=semantic,
            headers={"Idempotency-Key": "new"},
        ).status_code
        == 409
    )
    selection = {
        "asset": ref,
        "representation_id": "normalized",
        "semantic_revision": 1,
        "representation_digest": asset["manifest"]["representations"][1]["digest"],
        "field_bindings": {"groupField": "f0", "timeField": "f1", "valueField": "f2"},
    }
    assert client.post(P + "/resolve", json=selection).status_code == 200
    assert client.post("/internal/projects/other/resolve", json=selection).status_code == 403
    assert client.get(url.replace("/project/", "/other/")).status_code == 404
    assert client.get(url, headers={"Authorization": "Bearer invalid"}).status_code == 401
    with db.engine.connect() as c:
        assert c.scalar(sa.select(sa.func.count()).select_from(tables.assets)) == 1


def test_original_parquet_cannot_bind_normalized_field_ids(data_client):
    client, worker, _ = data_client
    sink = io.BytesIO()
    pq.write_table(pa.table({"station": ["A"], "date": ["2024-01-01"], "level": ["1"]}), sink)
    upload = finalized(client, worker, sink.getvalue(), "parquet", {"columns": []})
    ref = upload["asset"]
    asset = client.get(f"{P}/assets/{ref['asset_id']}/versions/1").json()
    assert (
        client.post(
            f"{P}/assets/{ref['asset_id']}/semantic-revisions",
            json={
                "expected_revision": 0,
                "version_id": "1",
                "semantics": {
                    "timezone": "UTC",
                    "value_unit": "m",
                    "quantity": "observation_value",
                },
            },
        ).status_code
        == 201
    )
    selection = {
        "asset": ref,
        "representation_id": "original",
        "representation_digest": asset["manifest"]["representations"][0]["digest"],
        "semantic_revision": 1,
        "field_bindings": {"groupField": "f0", "timeField": "f1", "valueField": "f2"},
    }
    response = client.post(P + "/resolve", json=selection)
    assert response.status_code == 409
    assert response.json()["code"] == "INVALID_BINDING"
    selection["representation_id"] = "normalized"
    selection["representation_digest"] = asset["manifest"]["representations"][1]["digest"]
    assert client.post(P + "/resolve", json=selection).status_code == 200


def test_upload_replay_bad_digest_partial_and_changed_content(data_client):
    client, worker, db = data_client
    id = uploaded(client)
    assert uploaded(client) == id
    for bad in (CONTENT[:-1], CONTENT[:-1] + b"X"):
        assert client.put(f"{P}/uploads/{id}/content", content=bad).status_code == 422
    assert client.put(f"{P}/uploads/{id}/content", content=CONTENT * 2).status_code == 413
    assert (
        client.post(
            P + "/uploads",
            headers={"Idempotency-Key": "create"},
            json={"filename": "other", "format": "csv", "size": 1, "digest": checksum(b"x")},
        ).status_code
        == 409
    )
    assert not worker.tick()


def test_import_lease_restart_fencing_and_atomic_visibility(data_client):
    client, worker, db = data_client
    id = uploaded(client)
    client.post(f"{P}/uploads/{id}/finalize", json=CONFIG)
    old = worker.repo.claim("dead-worker")
    assert client.get(P + "/assets").json()["items"] == []
    with db.engine.begin() as c:
        c.execute(
            tables.uploads.update().values(
                lease_until=sa.func.now() - sa.text("interval '1 second'")
            )
        )
    assert Worker(db, worker.blobs).tick()
    assert not worker.repo.import_result(old, error={"code": "old"})
    assert client.get(f"{P}/uploads/{id}").json()["status"] == "READY"
    assert len(client.get(P + "/assets").json()["items"]) == 1


def test_long_import_renews_lease_before_original_expiry(data_client, monkeypatch):
    import platform_data.worker as worker_module

    client, worker, db = data_client
    upload_id = uploaded(client)
    assert client.post(f"{P}/uploads/{upload_id}/finalize", json=CONFIG).status_code == 202
    entered, resume = threading.Event(), threading.Event()
    original = worker_module.normalize

    def slow_normalize(*args):
        entered.set()
        assert resume.wait(5)
        return original(*args)

    monkeypatch.setattr(worker_module, "normalize", slow_normalize)
    worker.lease_seconds, worker.heartbeat_seconds = 0.3, 0.08
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(worker.tick)
        assert entered.wait(5)
        time.sleep(0.75)
        assert worker.repo.claim("competing-worker", 0.3) is None
        resume.set()
        assert future.result(timeout=5)
    assert client.get(f"{P}/uploads/{upload_id}").json()["status"] == "READY"


@pytest.mark.parametrize(
    "content,config",
    [
        (b"a,a\n1,2\n", {"columns": [{"name": "a", "type": "string"}]}),
        (
            b"a,b\n1,not-number\n",
            {"columns": [{"name": "a", "type": "string"}, {"name": "b", "type": "float64"}]},
        ),
        (b"a\n\xff\n", {"columns": [{"name": "a", "type": "string"}]}),
        (b"a\n" + b"1\n" * 100001, {"columns": [{"name": "a", "type": "string"}]}),
    ],
    ids=["duplicate-columns", "invalid-number", "non-utf8", "row-limit"],
)
def test_invalid_table_is_never_published(data_client, content, config):
    client, worker, db = data_client
    upload = finalized(client, worker, content, config=config)
    assert upload["status"] == "FAILED"
    assert client.get(P + "/assets").json()["items"] == []


@pytest.mark.parametrize("nested", [False, True])
def test_parquet_flat_only(data_client, nested):
    client, worker, db = data_client
    table = pa.table({"station": [["A"]] if nested else ["A"], "value": [1.0]})
    file = io.BytesIO()
    pq.write_table(table, file)
    upload = finalized(client, worker, file.getvalue(), "parquet", {"columns": []})
    assert upload["status"] == ("FAILED" if nested else "READY")


def test_concurrent_finalization_is_idempotent(data_client):
    client, worker, db = data_client
    id = uploaded(client)
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(
            pool.map(
                lambda _: client.post(f"{P}/uploads/{id}/finalize", json=CONFIG).json(), range(4)
            )
        )
    assert all(value == responses[0] for value in responses)
    assert worker.tick()
    assert not worker.tick()
