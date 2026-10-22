import hashlib
import time

import sqlalchemy as sa
from fastapi.testclient import TestClient
from platform_contracts.service import ServiceFault
from platform_knowledge import tables
from platform_knowledge.api import create_app
from platform_knowledge.blobs import Blobs
from platform_knowledge.worker import Worker

TOKEN = "p" * 48
P = "/internal/projects/project/knowledge"


class FakeData:
    def __init__(self, raw, project="project", asset="asset"):
        self.raw = raw
        self.digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        self.ref = {
            "project_id": project,
            "asset_id": asset,
            "version_id": "1",
            "version_manifest_digest": "sha256:" + "a" * 64,
        }

    def asset(self, project, source):
        assert project == source["project_id"]
        return {
            "ref": source,
            "manifest": {
                "kind": "DOCUMENT",
                "title": "Groundwater guide.md",
                "representations": [
                    {
                        "representation_id": "original",
                        "format": "markdown",
                        "digest": self.digest,
                        "size": len(self.raw),
                    }
                ],
            },
            "semantic_revisions": [],
        }

    def content(self, project, source, representation):
        assert representation == "original"
        return self.raw, self.digest


def setup_knowledge(database, tmp_path, raw=None):
    raw = (
        raw
        or "# 地下水规范\r\n\r\n监测井水位高程采用 1985 国家高程基准。\r\n\r\nCode GW-42.".encode()
    )
    blobs = Blobs(tmp_path / "knowledge")
    data = FakeData(raw)
    app = create_app(database, blobs, {"platform": TOKEN})
    client = TestClient(
        app,
        headers={"Authorization": "Bearer " + TOKEN, "X-Actor-ID": "owner"},
    )
    return client, Worker(database, blobs, data), data, blobs


def create_import(client, source, key="import"):
    response = client.post(
        P + "/imports",
        headers={"Idempotency-Key": key},
        json={"source": source, "representation_id": "original"},
    )
    assert response.status_code == 202, response.text
    return response.json()


def test_import_search_exact_read_withdraw_and_historical_read(knowledge_database, tmp_path):
    client, worker, data, blobs = setup_knowledge(knowledge_database, tmp_path)
    with client:
        receipt = create_import(client, data.ref)
        assert create_import(client, data.ref) == receipt
        assert worker.tick()
        view = client.get(P + "/imports/" + receipt["import_id"]).json()
        assert view["status"] == "READY"
        assert view["document_id"] and view["version_id"] == "1"
        version_path = f"{P}/documents/{view['document_id']}/versions/1"
        version = client.get(version_path).json()
        assert version["availability"] == "ACTIVE"
        assert version["epistemic_status"] == "DECLARED"
        assert version["review_state"] == "UNREVIEWED"
        assert blobs.verify(version["content_digest"]).decode().startswith("# 地下水规范\n")

        result = client.post(P + "/search", json={"query": "水位高程", "limit": 20}).json()
        assert result["status"] == "RESULTS" and result["candidate_count"] >= 1
        hit = result["items"][0]
        assert "evidence_ref" not in hit
        read_path = version_path + "/units/" + hit["unit_id"]
        evidence = client.get(read_path).json()
        ref = evidence["evidence_ref"]
        normalized = blobs.verify(ref["content_digest"]).decode()
        assert normalized[ref["start_char"] : ref["end_char"]] == evidence["text"]
        assert "水位高程" in evidence["text"]

        withdrawn = client.post(
            version_path + "/withdraw",
            headers={"Idempotency-Key": "withdraw"},
            json={"expected_lifecycle_revision": 1, "reason": "Superseded source"},
        )
        assert withdrawn.status_code == 200, withdrawn.text
        assert withdrawn.json()["availability"] == "WITHDRAWN"
        assert (
            client.post(P + "/search", json={"query": "水位高程"}).json()["status"] == "NO_RESULTS"
        )
        historical = client.get(read_path).json()
        assert historical["availability"] == "WITHDRAWN"
        assert historical["lifecycle_revision"] == 2
        assert (
            client.post(
                version_path + "/withdraw",
                headers={"Idempotency-Key": "conflict"},
                json={"expected_lifecycle_revision": 1, "reason": "Again"},
            ).status_code
            == 409
        )

    with knowledge_database.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(tables.versions)) == 1
        assert connection.scalar(sa.select(sa.func.count()).select_from(tables.read_receipts)) == 2
        assert (
            connection.scalar(sa.select(sa.func.count()).select_from(tables.lifecycle_events)) == 2
        )
        assert connection.scalar(sa.select(sa.func.count()).select_from(tables.outbox)) == 2


def test_scope_idempotency_and_worker_generation_fencing(knowledge_database, tmp_path):
    client, worker, data, _ = setup_knowledge(knowledge_database, tmp_path)
    with client:
        receipt = create_import(client, data.ref)
        response = client.post(
            P + "/imports",
            headers={"Idempotency-Key": "import"},
            json={"source": data.ref | {"asset_id": "changed"}, "representation_id": "original"},
        )
        assert response.status_code == 409
        old = worker.repo.claim("dead-worker", 30)
        with knowledge_database.engine.begin() as connection:
            connection.execute(
                tables.imports.update().values(
                    lease_until=sa.func.now() - sa.text("interval '1 second'")
                )
            )
        assert worker.tick()
        assert not worker.repo.import_result(old, error={"code": "OLD"})
        view = client.get(P + "/imports/" + receipt["import_id"]).json()
        assert view["status"] == "READY"
        assert (
            client.post(
                "/internal/projects/other/knowledge/search", json={"query": "水位高程"}
            ).json()["status"]
            == "NO_RESULTS"
        )
        path = f"/internal/projects/other/knowledge/documents/{view['document_id']}/versions/1"
        assert client.get(path).status_code == 404


def test_document_list_pages_all_versions_and_rejects_foreign_cursor(knowledge_database, tmp_path):
    client, _, _, _ = setup_knowledge(knowledge_database, tmp_path)
    digest = "sha256:" + "a" * 64
    with knowledge_database.engine.begin() as connection:
        now = connection.scalar(sa.select(sa.func.clock_timestamp()))
        documents = [
            {"document_id": f"document-{index:03d}", "project_id": "project", "created_at": now}
            for index in range(101)
        ]
        versions = []
        for document in documents:
            document_id = document["document_id"]
            source_id = document_id.removeprefix("document-")
            source = {
                "project_id": "project",
                "asset_id": "asset-" + source_id,
                "version_id": "1",
                "version_manifest_digest": digest,
            }
            versions.append(
                {
                    "document_id": document_id,
                    "version_id": "1",
                    "project_id": "project",
                    "title": document_id,
                    "source": source,
                    "source_asset_id": source["asset_id"],
                    "source_version_id": source["version_id"],
                    "source_manifest_digest": digest,
                    "source_digest": digest,
                    "content_digest": digest,
                    "format": "text",
                    "availability": "ACTIVE",
                    "epistemic_status": "DECLARED",
                    "review_state": "UNREVIEWED",
                    "lifecycle_revision": 1,
                    "unit_count": 1,
                    "created_at": now,
                }
            )
        connection.execute(tables.documents.insert(), documents)
        connection.execute(tables.versions.insert(), versions)

    with client:
        first = client.get(P + "/documents").json()
        assert len(first["items"]) == 100 and first["next_cursor"]
        second = client.get(P + "/documents", params={"after": first["next_cursor"]}).json()
        assert [item["document_id"] for item in second["items"]] == ["document-100"]
        assert second["next_cursor"] is None
        assert client.get(P + "/documents", params={"after": "invalid!"}).status_code == 400
        assert (
            client.get(
                "/internal/projects/other/knowledge/documents",
                params={"after": first["next_cursor"]},
            ).status_code
            == 400
        )
        assert client.get(P + "/documents", params={"limit": 101}).status_code == 422


def test_scoped_search_filters_sources_before_applying_result_limit(knowledge_database, tmp_path):
    raw = b"scope needle approved"
    client, worker, data, _ = setup_knowledge(knowledge_database, tmp_path, raw=raw)
    with client:
        receipt = create_import(client, data.ref)
        assert worker.tick()
        assert client.get(P + "/imports/" + receipt["import_id"]).json()["status"] == "READY"

        text = raw.decode()
        exact_digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        hidden_source = {
            "project_id": "project",
            "asset_id": "hidden-asset",
            "version_id": "1",
            "version_manifest_digest": "sha256:" + "b" * 64,
        }
        with knowledge_database.engine.begin() as connection:
            now = connection.scalar(sa.select(sa.func.clock_timestamp()))
            connection.execute(
                tables.documents.insert().values(
                    document_id="hidden-document", project_id="project", created_at=now
                )
            )
            connection.execute(
                tables.versions.insert().values(
                    document_id="hidden-document",
                    version_id="1",
                    project_id="project",
                    title="Hidden",
                    source=hidden_source,
                    source_asset_id=hidden_source["asset_id"],
                    source_version_id=hidden_source["version_id"],
                    source_manifest_digest=hidden_source["version_manifest_digest"],
                    source_digest="sha256:" + "c" * 64,
                    content_digest="sha256:" + "d" * 64,
                    format="text",
                    availability="ACTIVE",
                    epistemic_status="DECLARED",
                    review_state="UNREVIEWED",
                    lifecycle_revision=1,
                    unit_count=20,
                    created_at=now,
                )
            )
            for index in range(20):
                connection.execute(
                    tables.units.insert().values(
                        unit_id=f"000-hidden-{index:02d}",
                        document_id="hidden-document",
                        version_id="1",
                        project_id="project",
                        start_char=index * 100,
                        end_char=index * 100 + len(text),
                        text=text,
                        exact_text_digest=exact_digest,
                        prefix="",
                        suffix="",
                        search_vector=sa.func.to_tsvector("simple", text),
                    )
                )

        global_result = client.post(P + "/search", json={"query": "scope needle", "limit": 20})
        assert global_result.status_code == 200
        assert global_result.json()["candidate_count"] >= 21
        assert {item["document_id"] for item in global_result.json()["items"]} == {
            "hidden-document"
        }

        scoped = client.post(
            P + "/search-scoped",
            json={"query": "scope needle", "limit": 20, "sources": [data.ref]},
        )
        assert scoped.status_code == 200, scoped.text
        assert scoped.json()["status"] == "RESULTS"
        assert scoped.json()["candidate_count"] == 1
        assert scoped.json()["items"][0]["document_id"] != "hidden-document"

        cross_project = client.post(
            P + "/search-scoped",
            json={
                "query": "scope needle",
                "limit": 20,
                "sources": [data.ref | {"project_id": "other"}],
            },
        )
        assert cross_project.status_code == 403


def test_transient_data_failure_requeues_without_fabricating_terminal_failure(
    knowledge_database, tmp_path
):
    client, worker, data, _ = setup_knowledge(knowledge_database, tmp_path)

    class UnavailableData:
        def asset(self, project, source):
            raise ServiceFault("DEPENDENCY_UNAVAILABLE", "temporarily unavailable", 503)

    with client:
        receipt = create_import(client, data.ref)
        worker.data = UnavailableData()
        assert worker.tick() is False
        pending = client.get(P + "/imports/" + receipt["import_id"]).json()
        assert pending["status"] == "QUEUED" and pending["error"] is None
        worker.data = data
        assert worker.tick() is True
        assert client.get(P + "/imports/" + receipt["import_id"]).json()["status"] == "READY"


def test_lexical_quality_and_ten_thousand_unit_latency(knowledge_database, tmp_path):
    client, worker, data, _ = setup_knowledge(knowledge_database, tmp_path)
    with client:
        receipt = create_import(client, data.ref)
        assert worker.tick()
        assert client.get(P + "/imports/" + receipt["import_id"]).json()["status"] == "READY"
        queries = ["地下水规范", "水位高程", "1985 国家高程基准", "GW-42"]
        assert all(
            client.post(P + "/search", json={"query": query}).json()["items"] for query in queries
        )

    now = sa.func.now()
    source = {
        "project_id": "project",
        "asset_id": "benchmark",
        "version_id": "1",
        "version_manifest_digest": "sha256:" + "b" * 64,
    }
    with knowledge_database.engine.begin() as connection:
        connection.execute(
            tables.documents.insert().values(
                document_id="benchmark", project_id="project", created_at=now
            )
        )
        connection.execute(
            tables.versions.insert().values(
                document_id="benchmark",
                version_id="1",
                project_id="project",
                title="Benchmark",
                source=source,
                source_asset_id="benchmark",
                source_version_id="1",
                source_manifest_digest=source["version_manifest_digest"],
                source_digest="sha256:" + "c" * 64,
                content_digest="sha256:" + "d" * 64,
                format="text",
                availability="ACTIVE",
                epistemic_status="DECLARED",
                review_state="UNREVIEWED",
                lifecycle_revision=1,
                unit_count=10_000,
                created_at=now,
            )
        )
        connection.execute(
            sa.text(
                """
                INSERT INTO evidence_units (
                  unit_id, document_id, version_id, project_id, start_char, end_char,
                  text, exact_text_digest, prefix, suffix, search_vector
                )
                SELECT 'bench' || lpad(i::text, 5, '0'), 'benchmark', '1', 'project',
                       i * 20, i * 20 + 18, 'unrelated monitoring note ' || i,
                       'sha256:' || repeat('e', 64), '', '',
                       to_tsvector('simple', 'unrelated monitoring note ' || i)
                FROM generate_series(1, 10000) AS i
                """
            )
        )
    durations = []
    repository = worker.repo
    expected_order = None
    for _ in range(8):
        started = time.perf_counter()
        count, rows = repository.search("project", "GW-42", 20)
        durations.append(time.perf_counter() - started)
        assert count >= 1 and rows[0]["document_id"] != "benchmark"
        order = [row["unit_id"] for row in rows]
        expected_order = expected_order or order
        assert order == expected_order
    assert max(durations) < 0.5, durations
