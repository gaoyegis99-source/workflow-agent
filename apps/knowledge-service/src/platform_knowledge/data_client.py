"""Authenticated Data port; Knowledge never reads Data tables or storage paths."""

import httpx
from platform_contracts.runtime_context import DeploymentContext
from platform_contracts.service import ServiceFault, deployment_headers


class DataClient:
    def __init__(self, url, token, *, deployment: DeploymentContext):
        if not url.startswith(("http://", "https://")) or len(token or "") < 32:
            raise RuntimeError("Explicit Data URL and Knowledge credential required")
        self.client = httpx.Client(
            base_url=url,
            headers=deployment_headers({"Authorization": "Bearer " + token}, deployment=deployment),
            timeout=30,
            trust_env=False,
        )

    def _request(self, method, path):
        try:
            response = self.client.request(method, path)
        except httpx.HTTPError as exc:
            raise ServiceFault("DEPENDENCY_UNAVAILABLE", "Data service unavailable", 503) from exc
        if response.status_code >= 400:
            if response.status_code == 404:
                raise ServiceFault("RESOURCE_NOT_FOUND", "Source asset not found", 404)
            raise ServiceFault("DEPENDENCY_UNAVAILABLE", "Data service unavailable", 503)
        return response

    def asset(self, project, source):
        return self._request(
            "GET",
            f"/internal/projects/{project}/assets/{source['asset_id']}/versions/{source['version_id']}",
        ).json()

    def content(self, project, source, representation):
        response = self._request(
            "GET",
            f"/internal/projects/{project}/assets/{source['asset_id']}/versions/{source['version_id']}"
            f"/representations/{representation}/content",
        )
        return response.content, response.headers.get("digest")

    def close(self):
        self.client.close()
