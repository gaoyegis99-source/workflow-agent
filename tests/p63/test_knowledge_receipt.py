import sqlalchemy as sa
from platform_knowledge import tables

from tests.p51.test_knowledge import P, create_import, setup_knowledge


def test_exact_evidence_read_uses_stable_agent_receipt(knowledge_database, tmp_path):
    client, worker, data, _ = setup_knowledge(knowledge_database, tmp_path)
    with client:
        imported = create_import(client, data.ref)
        assert worker.tick()
        view = client.get(P + "/imports/" + imported["import_id"]).json()
        result = client.post(P + "/search", json={"query": "水位高程", "limit": 20}).json()
        hit = result["items"][0]
        path = f"{P}/documents/{view['document_id']}/versions/1/units/{hit['unit_id']}"
        headers = {"X-Read-Receipt-ID": "tr" + "a" * 62}
        first = client.get(path, headers=headers)
        second = client.get(path, headers=headers)
        assert first.status_code == second.status_code == 200
        assert first.json() == second.json()
        assert first.json()["read_receipt_id"] == headers["X-Read-Receipt-ID"]
    with knowledge_database.engine.connect() as connection:
        count = connection.scalar(sa.select(sa.func.count()).select_from(tables.read_receipts))
    assert count == 1
