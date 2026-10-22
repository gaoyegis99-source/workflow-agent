# ruff: noqa: F811

"""Real-browser pagination smoke test using an isolated HTTP service fixture.

Unlike the retained production probe, this test does not use Playwright
``page.route`` interception. The browser talks to a real Platform API process,
which talks over HTTP to isolated Data/Knowledge/Execution fixtures.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from platform_core.application.control import ControlService
from playwright.sync_api import expect, sync_playwright

from tests.p2.conftest import database, owner, postgres  # noqa: F401
from tests.p2.processes import Processes, eventually


def _asset(index: int, project_id: str, *, detail: bool = False) -> dict:
    value = {
        "ref": {
            "project_id": project_id,
            "asset_id": f"asset-{index:03d}",
            "version_id": f"version-{index:03d}",
            "version_manifest_digest": "sha256:" + f"{index:064x}",
        },
        "manifest": {
            "kind": "SPATIAL_VECTOR",
            "row_count": index,
            "representations": [],
            "spatial": {"layers": []},
        },
    }
    if detail:
        value["semantic_revisions"] = []
    return value


class _ServiceHandler(BaseHTTPRequestHandler):
    project_id = ""
    requests: list[str] = []

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        self.requests.append(parsed.path + ("?" + parsed.query if parsed.query else ""))
        path = parsed.path
        prefix = f"/internal/projects/{self.project_id}"
        if path == prefix + "/assets":
            after = parse_qs(parsed.query).get("after", [None])[0]
            payload = (
                {"items": [_asset(100, self.project_id)], "next_cursor": None}
                if after == "page-2"
                else {
                    "items": [_asset(index, self.project_id) for index in range(100)],
                    "next_cursor": "page-2",
                }
            )
            self._json(200, payload)
            return
        if path == prefix + "/assets/asset-100/versions/version-100":
            self._json(200, _asset(100, self.project_id, detail=True))
            return
        if path == prefix + "/uploads":
            self._json(200, {"items": [], "next_cursor": None})
            return
        if path == f"/internal/projects/{self.project_id}/knowledge/documents":
            self._json(200, {"items": [], "next_cursor": None})
            return
        self._json(404, {"code": "RESOURCE_NOT_FOUND", "message": "Not found"})

    def _json(self, status: int, value: dict):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def test_browser_pagination_uses_real_http_gateway_and_service_fixture(database, owner, tmp_path):
    actor, token = owner
    project = ControlService(database).create_project(actor, "browser-http", "Browser HTTP")
    project_id = project["project_id"]
    _ServiceHandler.project_id = project_id
    _ServiceHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ServiceHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    service_url = f"http://127.0.0.1:{server.server_port}"
    processes = Processes(tmp_path, database.engine.url.render_as_string(hide_password=False))
    client = None
    try:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        processes.start(
            [sys.executable, "-m", "platform_api", "--port", str(port)],
            {
                "V7_DEPLOYMENT_MODE": "development",
                "V7_DATA_URL": service_url,
                "V7_PLATFORM_DATA_TOKEN": "d" * 32,
                "V7_EXECUTION_URL": service_url,
                "V7_PLATFORM_EXECUTION_TOKEN": "e" * 32,
                "V7_KNOWLEDGE_URL": service_url,
                "V7_PLATFORM_KNOWLEDGE_TOKEN": "k" * 32,
            },
        )
        client = httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=5)

        def ready():
            try:
                return client.get("/api/v1/ready").status_code == 200
            except httpx.TransportError:
                return False

        eventually(ready, timeout=20)
        origin = str(client.base_url).rstrip("/")
        api_assets = client.get(
            f"/api/v1/projects/{project_id}/assets",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert api_assets.status_code == 200, api_assets.text
        assert api_assets.json()["items"][0]["ref"]["asset_id"] == "asset-000"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.goto(origin + "/")
            page.get_by_label("临时 Bearer Token").fill(token)
            page.get_by_role("button", name="连接并加载项目").click()
            page.get_by_role("button", name="进入工作区").click()

            page.get_by_role("link", name="数据资产").click()
            expect(page.get_by_text("asset-000", exact=True)).to_be_visible()
            expect(page.get_by_text("asset-100", exact=True)).to_have_count(0)
            page.get_by_role("button", name="继续加载").click()
            expect(page.get_by_text("asset-100", exact=True)).to_be_visible()
            page.get_by_text("asset-100", exact=True).click()
            expect(page).to_have_url(
                origin + f"/projects/{project_id}/assets/asset-100?version=version-100"
            )
            expect(page.get_by_role("heading", name="asset-100")).to_be_visible()
            browser.close()
        evidence_path = os.environ.get("V7_BROWSER_HTTP_EVIDENCE")
        if evidence_path:
            Path(evidence_path).parent.mkdir(parents=True, exist_ok=True)
            Path(evidence_path).write_text(
                json.dumps(
                    {
                        "status": "PASS",
                        "evidence_kind": "real_platform_api_with_http_service_fixtures",
                        "transport": (
                            "Playwright TCP browser to Platform API; isolated HTTP "
                            "Data/Knowledge/Execution fixtures"
                        ),
                        "asset_catalog_101": True,
                        "second_page_cursor_used": True,
                        "fixed_version_detail": True,
                        "production_business_data_read": False,
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
    finally:
        if client is not None:
            client.close()
        processes.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert any("after=page-2" in path for path in _ServiceHandler.requests)
