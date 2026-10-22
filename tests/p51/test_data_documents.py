import hashlib

import sqlalchemy as sa
from fastapi.testclient import TestClient
from platform_data.adapters import tables
from platform_data.adapters.blobs import Blobs
from platform_data.api import create_app
from platform_data.document_worker import DocumentWorker

TOKEN = "p" * 48
P = "/internal/projects/project"


def upload(client, raw, format_id="markdown", key="create"):
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    response = client.post(
        P + "/uploads",
        headers={"Idempotency-Key": key},
        json={
            "filename": "guide.md" if format_id == "markdown" else "guide.txt",
            "format": format_id,
            "size": len(raw),
            "digest": digest,
        },
    )
    assert response.status_code == 201, response.text
    upload_id = response.json()["upload_id"]
    assert client.put(f"{P}/uploads/{upload_id}/content", content=raw).status_code == 200
    response = client.post(
        f"{P}/uploads/{upload_id}/finalize",
        headers={"Idempotency-Key": "finalize-" + key},
        json={"kind": "DOCUMENT", "profile": "utf8-text-v1"},
    )
    assert response.status_code == 202, response.text
    return upload_id, digest


def test_document_worker_preserves_raw_bytes_and_publishes_document_manifest(
    data_database, tmp_path
):
    blobs = Blobs(tmp_path / "data")
    app = create_app(
        data_database,
        blobs,
        {"platform": TOKEN, "execution": "e" * 48, "knowledge": "k" * 48},
    )
    raw = b"\xef\xbb\xbf# Guide\r\nGroundwater evidence\r\n"
    with TestClient(
        app,
        headers={"Authorization": "Bearer " + TOKEN, "X-Actor-ID": "owner"},
    ) as client:
        upload_id, digest = upload(client, raw)
        worker = DocumentWorker(data_database, blobs)
        assert worker.tick()
        assert not worker.tick()
        view = client.get(f"{P}/uploads/{upload_id}").json()
        assert view["status"] == "READY"
        ref = view["asset"]
        asset = client.get(f"{P}/assets/{ref['asset_id']}/versions/1").json()
        assert asset["manifest"]["kind"] == "DOCUMENT"
        facts = asset["manifest"]["document"]
        assert facts["encoding"] == "utf-8" and facts["utf8_bom"] is True
        representation = asset["manifest"]["representations"][0]
        assert representation["digest"] == digest
        response = client.get(
            f"{P}/assets/{ref['asset_id']}/versions/1/representations/original/content",
            headers={"Authorization": "Bearer " + "k" * 48},
        )
        assert response.content == raw
        assert response.headers["digest"] == digest


def test_invalid_utf8_binary_and_wrong_finalize_never_publish(data_database, tmp_path):
    blobs = Blobs(tmp_path / "data")
    app = create_app(data_database, blobs, {"platform": TOKEN})
    with TestClient(
        app,
        headers={"Authorization": "Bearer " + TOKEN, "X-Actor-ID": "owner"},
    ) as client:
        for index, raw in enumerate((b"bad\xff", b"text\x00binary", b" \n\t")):
            upload_id, _ = upload(client, raw, "text", "bad" + str(index))
            assert DocumentWorker(data_database, blobs).tick()
            assert client.get(f"{P}/uploads/{upload_id}").json()["status"] == "FAILED"
        assert client.get(P + "/assets").json()["items"] == []
    with data_database.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(tables.assets)) == 0


def test_upload_and_asset_lists_page_past_100_with_scoped_composite_cursors(
    data_database, tmp_path
):
    app = create_app(data_database, Blobs(tmp_path / "data"), {"platform": TOKEN})
    digest = "sha256:" + "a" * 64
    upload_rows = []
    asset_rows = []
    for index in range(101):
        identity = f"item-{index:03d}"
        upload_rows.append(
            {
                "upload_id": identity,
                "project_id": "project",
                "state": "AWAITING_CONTENT",
                "payload": {
                    "actor_id": "owner",
                    "filename": identity + ".txt",
                    "format": "text",
                    "size": 1,
                    "digest": digest,
                },
            }
        )
        asset_rows.append(
            {
                "asset_id": identity,
                "version_id": "1",
                "project_id": "project",
                "manifest": {"kind": "DOCUMENT", "title": identity},
                "manifest_digest": digest,
            }
        )
    # A second version proves the cursor sorts on both identity components.
    asset_rows.append(asset_rows[50] | {"version_id": "2"})
    with data_database.engine.begin() as connection:
        connection.execute(tables.uploads.insert(), upload_rows)
        connection.execute(tables.assets.insert(), asset_rows)

    with TestClient(
        app, headers={"Authorization": "Bearer " + TOKEN, "X-Actor-ID": "owner"}
    ) as client:
        first_uploads = client.get(P + "/uploads").json()
        assert len(first_uploads["items"]) == 100 and first_uploads["next_cursor"]
        assert "=" not in first_uploads["next_cursor"]
        for limit in (1, 50, 51, 100):
            page = client.get(P + "/uploads", params={"limit": limit}).json()
            assert len(page["items"]) == limit and page["next_cursor"]
        second_uploads = client.get(
            P + "/uploads", params={"after": first_uploads["next_cursor"]}
        ).json()
        assert [item["upload_id"] for item in second_uploads["items"]] == ["item-100"]
        assert second_uploads["next_cursor"] is None

        first_assets = client.get(P + "/assets", params={"limit": 51}).json()
        first_identities = [
            (item["ref"]["asset_id"], item["ref"]["version_id"]) for item in first_assets["items"]
        ]
        assert first_identities[-2:] == [
            ("item-049", "1"),
            ("item-050", "1"),
        ]
        second_assets = client.get(
            P + "/assets",
            params={"limit": 51, "after": first_assets["next_cursor"]},
        ).json()
        identities = [
            (item["ref"]["asset_id"], item["ref"]["version_id"])
            for item in first_assets["items"] + second_assets["items"]
        ]
        assert len(identities) == 102
        assert identities[51:53] == [("item-050", "2"), ("item-051", "1")]
        assert second_assets["next_cursor"] is None

        assert client.get(P + "/assets", params={"after": "invalid!"}).status_code == 400
        assert (
            client.get(P + "/assets", params={"after": first_uploads["next_cursor"]}).status_code
            == 400
        )
        assert client.get(P + "/assets", params={"limit": 101}).status_code == 422
        assert client.get(P + "/assets", params={"limit": 0}).status_code == 422
        assert client.get("/internal/projects/empty/assets").json() == {
            "items": [],
            "next_cursor": None,
        }
