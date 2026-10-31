"""Test-only HTTP ExecutionPort; production has no mock adapter import."""

import http.client
import json
import urllib.error
import urllib.request

from platform_core.domain.control import ExternalObservation


class PeerClient:
    def __init__(self, base: str, token: str):
        self.base, self.token = base, token
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, method: str, path: str, body: dict | None = None):
        request = urllib.request.Request(
            self.base + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": "Bearer " + self.token, "Content-Type": "application/json"},
        )
        try:
            with self.http.open(request, timeout=5) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise OSError(f"Peer returned HTTP {error.code}") from None
        except (urllib.error.URLError, http.client.HTTPException) as error:
            raise OSError("Peer response unknown") from error

    def submit(self, action_id, request):
        return ExternalObservation(**self.request("PUT", "/actions/" + action_id, request))

    def query(self, action_id):
        value = self.request("GET", "/actions/" + action_id)
        return ExternalObservation(**value) if value else None

    def cancel(self, action_id):
        return ExternalObservation(**self.request("DELETE", "/actions/" + action_id))
