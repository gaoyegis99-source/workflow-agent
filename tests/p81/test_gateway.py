import httpx
from fastapi.testclient import TestClient
from platform_api.gateway import create_gateway


def test_write_barrier_blocks_mutations_but_keeps_reads_available(tmp_path, monkeypatch):
    monkeypatch.setenv("V7_RELEASE_DIGEST", "sha256:" + "a" * 64)
    observed = []

    def upstream(request):
        observed.append(request)
        return httpx.Response(
            200,
            json={"method": request.method, "path": request.url.path},
            headers={
                "X-Upstream": "yes",
                "Date": "Fri, 16 Oct 2026 00:00:00 GMT",
                "Server": "upstream-test",
            },
        )

    barrier = tmp_path / "control" / "maintenance"
    barrier.parent.mkdir()
    app = create_gateway(
        "http://platform.invalid",
        barrier,
        transport=httpx.MockTransport(upstream),
    )
    with TestClient(app) as client:
        health = client.get("/gateway/health")
        assert health.json() == {
            "status": "ok",
            "maintenance": False,
        }
        assert len(health.headers["X-Request-ID"]) == 32
        assert health.headers["Cache-Control"] == "no-store"
        assert health.headers["X-V7-Release-Digest"] == "sha256:" + "a" * 64
        response = client.post(
            "/api/v1/projects?source=test",
            headers={"Authorization": "Bearer secret", "Idempotency-Key": "one"},
            json={"name": "before"},
        )
        assert response.status_code == 200
        assert observed[-1].headers["idempotency-key"] == "one"
        assert observed[-1].url.query == b"source=test"

        barrier.touch(mode=0o600)
        blocked = client.delete("/api/v1/projects/project-1")
        assert blocked.status_code == 503
        assert blocked.headers["Retry-After"] == "30"
        assert blocked.json()["code"] == "MAINTENANCE_IN_PROGRESS"
        assert len(observed) == 1

        read = client.get("/api/v1/projects/project-1")
        assert read.status_code == 200
        assert read.headers["X-Upstream"] == "yes"
        assert "date" not in read.headers
        assert "server" not in read.headers
        assert read.json()["method"] == "GET"


def test_gateway_reports_upstream_failure_without_exposing_details(tmp_path):
    def unavailable(_request):
        raise httpx.ConnectError("private upstream detail")

    app = create_gateway(
        "http://platform.invalid",
        tmp_path / "maintenance",
        transport=httpx.MockTransport(unavailable),
    )
    with TestClient(app) as client:
        response = client.get("/api/v1/projects")
        assert response.status_code == 503
        assert response.json() == {
            "code": "SERVICE_UNAVAILABLE",
            "message": "The platform API is temporarily unavailable",
            "retry_mode": "SAME_KEY",
        }
        assert len(response.headers["X-Request-ID"]) == 32
        assert response.headers["Cache-Control"] == "no-store"
