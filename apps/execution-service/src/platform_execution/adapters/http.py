"""Fixed authenticated service endpoints; no service URL is accepted in an Action."""

import httpx
from platform_contracts.service import ServiceFault, release_headers


class Client:
    def __init__(self, url, token):
        if not url.startswith(("http://", "https://")) or len(token or "") < 32:
            raise ValueError("Explicit service URL and credential required")
        self.client = httpx.Client(
            base_url=url,
            headers=release_headers({"Authorization": "Bearer " + token}),
            timeout=5,
            trust_env=False,
        )

    def request(self, method, path, **kwargs):
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise OSError("Service communication failed") from exc
        if response.status_code >= 400:
            try:
                value = response.json()
            except ValueError:
                value = {}
            raise ServiceFault(
                value.get("code", "DEPENDENCY_UNAVAILABLE"),
                "Service request failed",
                response.status_code,
            )
        return response

    def json(self, method, path, **kwargs):
        return self.request(method, path, **kwargs).json()

    def close(self):
        self.client.close()
