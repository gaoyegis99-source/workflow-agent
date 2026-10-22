import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import time

import httpx
import sqlalchemy as sa

from tests.p2.conftest import database, owner  # noqa: F401


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_ready(url):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            if httpx.get(url, timeout=1, trust_env=False).status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.1)
    raise AssertionError("service did not become ready: " + url)


def eventually(call, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = call()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("condition did not converge")


def test_real_http_document_to_exact_evidence_and_current_roles(  # noqa: F811
    database,  # noqa: F811
    owner,  # noqa: F811
    data_database,
    knowledge_database,
    tmp_path,
):
    actor, owner_token = owner
    from platform_core.application.control import ControlService

    control = ControlService(database)
    project = control.create_project(actor, "p51-http-project", "P5.1 HTTP")["project_id"]
    viewer_id = database.create_principal("viewer")
    viewer = database.issue_token(viewer_id)["token"]
    database.set_member(project, viewer_id, "VIEWER", actor=actor)

    data_port, knowledge_port, platform_port = free_port(), free_port(), free_port()
    platform_data = secrets.token_urlsafe(32)
    knowledge_data = secrets.token_urlsafe(32)
    platform_knowledge = secrets.token_urlsafe(32)
    env = os.environ.copy() | {
        "V7_DATABASE_URL": database.engine.url.render_as_string(hide_password=False),
        "V7_DATA_DATABASE_URL": data_database.engine.url.render_as_string(hide_password=False),
        "V7_KNOWLEDGE_DATABASE_URL": knowledge_database.engine.url.render_as_string(
            hide_password=False
        ),
        "V7_DATA_STORAGE": str(tmp_path / "data"),
        "V7_KNOWLEDGE_STORAGE": str(tmp_path / "knowledge"),
        "V7_DATA_URL": f"http://127.0.0.1:{data_port}",
        "V7_EXECUTION_URL": "http://127.0.0.1:9",
        "V7_KNOWLEDGE_URL": f"http://127.0.0.1:{knowledge_port}",
        "V7_PLATFORM_DATA_TOKEN": platform_data,
        "V7_KNOWLEDGE_DATA_TOKEN": knowledge_data,
        "V7_PLATFORM_KNOWLEDGE_TOKEN": platform_knowledge,
        "V7_PLATFORM_EXECUTION_TOKEN": secrets.token_urlsafe(32),
        "V7_DATA_PORT": str(data_port),
        "V7_KNOWLEDGE_PORT": str(knowledge_port),
        "V7_PLATFORM_PORT": str(platform_port),
    }
    commands = [
        [sys.executable, "-c", "from platform_data.api import main; main()"],
        [sys.executable, "-c", "from platform_knowledge.api import main; main()"],
        [sys.executable, "-m", "platform_api", "--port", str(platform_port)],
    ]
    processes = [subprocess.Popen(command, env=env, text=True) for command in commands]
    workers = []
    try:
        wait_ready(f"http://127.0.0.1:{data_port}/readyz")
        wait_ready(f"http://127.0.0.1:{knowledge_port}/readyz")
        wait_ready(f"http://127.0.0.1:{platform_port}/api/v1/ready")
        workers = [
            subprocess.Popen(
                [sys.executable, "-c", "from platform_data.document_worker import main; main()"],
                env=env,
                text=True,
            ),
            subprocess.Popen(
                [sys.executable, "-c", "from platform_knowledge.worker import main; main()"],
                env=env,
                text=True,
            ),
        ]
        base = f"http://127.0.0.1:{platform_port}/api/v1/projects/{project}"
        # All bytes and state cross real authenticated HTTP boundaries.
        client = httpx.Client(
            base_url=base,
            headers={"Authorization": "Bearer " + owner_token},
            timeout=10,
            trust_env=False,
        )
        raw = "# Groundwater\n\nExact evidence 水位高程 GW-42.\n".encode()
        create = client.post(
            "/uploads",
            headers={"Idempotency-Key": "document-create"},
            json={
                "filename": "guide.md",
                "format": "markdown",
                "size": len(raw),
                "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
            },
        )
        assert create.status_code == 201, create.text
        upload = create.json()
        path = "/uploads/" + upload["upload_id"]
        assert client.put(path + "/content", content=raw).status_code == 200
        assert (
            client.post(
                path + "/finalize",
                headers={"Idempotency-Key": "document-finalize"},
                json={"kind": "DOCUMENT", "profile": "utf8-text-v1"},
            ).status_code
            == 202
        )

        def imported_asset():
            value = client.get(path).json()
            assert value["status"] != "FAILED", value
            return value.get("asset") if value["status"] == "READY" else None

        asset = eventually(imported_asset)
        request_body = json.dumps(
            {"source": asset, "representation_id": "original"}, separators=(",", ":")
        ).encode()
        import_path = f"/api/v1/projects/{project}/knowledge/imports"
        raw_request = (
            f"POST {import_path} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{platform_port}\r\n"
            f"Authorization: Bearer {owner_token}\r\n"
            "Idempotency-Key: knowledge-import\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(request_body)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode() + request_body
        # Send the complete request and deliberately discard the response.
        with socket.create_connection(("127.0.0.1", platform_port), timeout=5) as dropped:
            dropped.sendall(raw_request)

        def committed_receipt():
            with knowledge_database.engine.connect() as connection:
                return connection.scalar(
                    sa.text("SELECT result FROM knowledge_idempotency WHERE key='knowledge-import'")
                )

        original_receipt = eventually(committed_receipt)
        processes[2].terminate()
        processes[2].wait(timeout=10)
        processes[2] = subprocess.Popen(commands[2], env=env, text=True)
        wait_ready(f"http://127.0.0.1:{platform_port}/api/v1/ready")
        client.close()
        client = httpx.Client(
            base_url=base,
            headers={"Authorization": "Bearer " + owner_token},
            timeout=10,
            trust_env=False,
        )
        accepted = client.post(
            "/knowledge/imports",
            headers={"Idempotency-Key": "knowledge-import"},
            json={"source": asset, "representation_id": "original"},
        )
        assert accepted.status_code == 202, accepted.text
        assert accepted.json()["import_id"] == original_receipt["import_id"]
        import_id = accepted.json()["import_id"]

        def imported_knowledge():
            value = client.get("/knowledge/imports/" + import_id).json()
            assert value["status"] != "FAILED", value
            return value if value["status"] == "READY" else None

        knowledge = eventually(imported_knowledge)
        viewer_client = httpx.Client(
            base_url=base,
            headers={"Authorization": "Bearer " + viewer},
            timeout=10,
            trust_env=False,
        )
        search = viewer_client.post("/knowledge/search", json={"query": "水位高程"})
        assert search.status_code == 200 and search.json()["status"] == "RESULTS"
        hit = search.json()["items"][0]
        evidence = viewer_client.get(
            f"/knowledge/documents/{hit['document_id']}/versions/1/units/{hit['unit_id']}"
        )
        assert evidence.status_code == 200 and "水位高程" in evidence.json()["text"]
        assert (
            viewer_client.post(
                "/knowledge/imports",
                headers={"Idempotency-Key": "viewer-import"},
                json={"source": asset, "representation_id": "original"},
            ).status_code
            == 403
        )
        version_path = f"/knowledge/documents/{knowledge['document_id']}/versions/1"
        assert (
            viewer_client.post(
                version_path + "/withdraw",
                headers={"Idempotency-Key": "viewer-withdraw"},
                json={"expected_lifecycle_revision": 1, "reason": "not allowed"},
            ).status_code
            == 403
        )
        assert (
            client.post(
                version_path + "/withdraw",
                headers={"Idempotency-Key": "owner-withdraw"},
                json={"expected_lifecycle_revision": 1, "reason": "superseded"},
            ).status_code
            == 200
        )
        assert (
            viewer_client.post("/knowledge/search", json={"query": "水位高程"}).json()["status"]
            == "NO_RESULTS"
        )
        historical = viewer_client.get(
            f"/knowledge/documents/{hit['document_id']}/versions/1/units/{hit['unit_id']}"
        )
        assert historical.status_code == 200
        assert historical.json()["availability"] == "WITHDRAWN"
        processes[1].terminate()
        processes[1].wait(timeout=10)
        unavailable = viewer_client.post("/knowledge/search", json={"query": "水位高程"})
        assert unavailable.status_code == 503
        assert unavailable.json()["code"] == "DEPENDENCY_UNAVAILABLE"
        client.close()
        viewer_client.close()
    finally:
        for process in [*workers, *processes]:
            if process.poll() is None:
                process.terminate()
        for process in [*workers, *processes]:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            if process.returncode not in {0, -15}:
                raise AssertionError(f"subprocess exited with {process.returncode}")
