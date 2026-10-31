"""Production HTTP adapters built from explicit composition values."""

import httpx
from platform_contracts.runtime_context import DeploymentContext, ServiceEndpoint
from platform_contracts.service import deployment_headers
from platform_core.domain.control import ExternalObservation, RuleViolation

TRANSFER_TIMEOUT = httpx.Timeout(30, connect=5)


class Remote:
    def __init__(self, url, token, *, deployment: DeploymentContext):
        if not url.startswith(("http://", "https://")) or len(token or "") < 32:
            raise RuntimeError("Explicit service URL and credential required")
        self.url, self.headers = (
            url.rstrip("/"),
            deployment_headers({"Authorization": "Bearer " + token}, deployment=deployment),
        )
        self.client = httpx.Client(base_url=url, headers=self.headers, timeout=5, trust_env=False)

    @staticmethod
    def check(response):
        if response.status_code >= 400:
            codes = {
                "MALFORMED_REQUEST",
                "AUTHENTICATION_REQUIRED",
                "SCOPE_DENIED",
                "RESOURCE_NOT_FOUND",
                "REVISION_CONFLICT",
                "IDEMPOTENCY_CONFLICT",
                "PRECONDITION_UNRESOLVED",
                "INVALID_BINDING",
                "UNSUPPORTED_TARGET",
            }
            try:
                code = response.json().get("code")
            except ValueError:
                code = None
            if code not in codes or response.status_code >= 500:
                raise RuleViolation(
                    "DEPENDENCY_UNAVAILABLE", "Service unavailable", 503, "SAME_KEY"
                )
            raise RuleViolation(code, "Service rejected the request", response.status_code)
        return response

    def request(self, method, path, **kwargs):
        try:
            return self.check(self.client.request(method, path, **kwargs))
        except httpx.HTTPError as exc:
            raise RuleViolation(
                "DEPENDENCY_UNAVAILABLE", "Service unavailable", 503, "SAME_KEY"
            ) from exc

    def json(self, method, path, **kwargs):
        return self.request(method, path, **kwargs).json()

    async def upload(self, path, stream):
        try:
            async with httpx.AsyncClient(
                base_url=self.url,
                headers=self.headers,
                timeout=TRANSFER_TIMEOUT,
                trust_env=False,
            ) as client:
                return self.check(await client.put(path, content=stream)).json()
        except httpx.HTTPError as exc:
            raise RuleViolation(
                "DEPENDENCY_UNAVAILABLE", "Data unavailable", 503, "SAME_KEY"
            ) from exc

    async def download(self, path):
        client = httpx.AsyncClient(
            base_url=self.url,
            headers=self.headers,
            timeout=TRANSFER_TIMEOUT,
            trust_env=False,
        )
        try:
            response = await client.send(client.build_request("GET", path), stream=True)
            if response.status_code >= 400:
                await response.aread()
            self.check(response)
        except BaseException:
            await client.aclose()
            raise

        async def body():
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            finally:
                await response.aclose()
                await client.aclose()

        return body(), response.headers.get("digest"), response.headers.get("content-type")

    def close(self):
        self.client.close()


class ExecutionClient(Remote):
    def submit(self, action_id, request):
        return ExternalObservation(
            **self.json("POST", "/internal/actions/" + action_id, json=request)
        )

    def query(self, action_id):
        try:
            return ExternalObservation(**self.json("GET", "/internal/actions/" + action_id))
        except RuleViolation as exc:
            if exc.status == 404:
                return None
            raise

    def cancel(self, action_id):
        return ExternalObservation(
            **self.json("POST", "/internal/actions/" + action_id + "/cancel")
        )


def data_client(
    endpoint: ServiceEndpoint | None, *, deployment: DeploymentContext
) -> Remote | None:
    return None if endpoint is None else Remote(endpoint.url, endpoint.token, deployment=deployment)


def execution_client(
    endpoint: ServiceEndpoint | None, *, deployment: DeploymentContext
) -> ExecutionClient | None:
    return (
        None
        if endpoint is None
        else ExecutionClient(endpoint.url, endpoint.token, deployment=deployment)
    )


def knowledge_client(
    endpoint: ServiceEndpoint | None, *, deployment: DeploymentContext
) -> Remote | None:
    return None if endpoint is None else Remote(endpoint.url, endpoint.token, deployment=deployment)
