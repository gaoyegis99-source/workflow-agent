"""Authenticated, redirect-free client for the frozen P4.3 remote protocol."""

import hashlib
import json
import os
import ssl
import stat
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from platform_contracts.service import ServiceFault, ensure


class CredentialProvider:
    def __init__(self, values=None, credential_dir=None, ca_dir=None):
        self.values = (
            values
            if values is not None
            else json.loads(os.environ.get("V7_REMOTE_CREDENTIALS_JSON", "{}"))
        )
        self.credential_dir = Path(
            credential_dir or os.environ.get("V7_REMOTE_CREDENTIAL_DIR", "/run/secrets/v7-remote")
        )
        self.ca_dir = Path(ca_dir or os.environ.get("V7_REMOTE_CA_DIR", "/etc/v7/remote-ca"))

    @staticmethod
    def file(root, reference, *, secret):
        path = root / reference
        ensure(
            path.is_file() and not path.is_symlink(),
            "DEPENDENCY_UNAVAILABLE",
            "Remote credential material is unavailable",
            503,
        )
        if secret:
            ensure(
                stat.S_IMODE(path.stat().st_mode) & 0o077 == 0,
                "DEPENDENCY_UNAVAILABLE",
                "Remote credential permissions are too broad",
                503,
            )
        return path

    def get(self, reference):
        value = self.values.get(reference)
        if value is None and self.credential_dir.is_dir():
            value = self.file(self.credential_dir, reference, secret=True).read_text().strip()
        ensure(
            isinstance(value, str) and len(value) >= 32,
            "DEPENDENCY_UNAVAILABLE",
            "Remote credential is unavailable",
            503,
        )
        return value

    def ca(self, reference):
        return str(self.file(self.ca_dir, reference, secret=False))


class RemoteClient:
    def __init__(self, binding, credentials):
        endpoint = binding["endpoint"].rstrip("/")
        parsed = urlsplit(endpoint)
        ensure(
            parsed.scheme in {"http", "https"} and parsed.hostname,
            "INVALID_BINDING",
            "Invalid frozen remote origin",
        )
        protocol = binding.get("protocol", "v7-remote-execution-v1")
        verify = True
        if protocol == "v7-remote-execution-v2":
            ensure(
                parsed.scheme == "https" and isinstance(binding.get("tls_ca_ref"), str),
                "INVALID_BINDING",
                "Remote v2 requires verified HTTPS",
            )
            verify = ssl.create_default_context(cafile=credentials.ca(binding["tls_ca_ref"]))
        self.origin = (parsed.scheme, parsed.hostname, parsed.port)
        self.client = httpx.Client(
            base_url=endpoint,
            headers={"Authorization": "Bearer " + credentials.get(binding["credential_ref"])},
            timeout=httpx.Timeout(30, connect=5),
            trust_env=False,
            follow_redirects=False,
            verify=verify,
        )

    def request(self, method, path, **kwargs):
        try:
            response = self.client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise OSError("Remote communication failed") from exc
        if 300 <= response.status_code < 400:
            raise ServiceFault("INVALID_BINDING", "Remote redirects are forbidden", 502)
        if response.status_code >= 400:
            if response.status_code == 404:
                return None
            try:
                body = response.json()
            except ValueError:
                body = {}
            raise ServiceFault(
                body.get("code", "DEPENDENCY_UNAVAILABLE"),
                "Remote service request failed",
                response.status_code,
            )
        return response

    def upload(self, action, port, content, digest):
        return self.request(
            "PUT",
            f"/v1/actions/{action}/inputs/{port}",
            content=content,
            headers={"Digest": digest, "Content-Length": str(len(content))},
        )

    def submit(self, action, body):
        response = self.request("POST", f"/v1/actions/{action}/submit", json=body)
        return None if response is None else response.json()

    def query(self, action):
        response = self.request("GET", f"/v1/actions/{action}")
        return None if response is None else response.json()

    def cancel(self, action):
        response = self.request("POST", f"/v1/actions/{action}/cancel")
        return None if response is None else response.json()

    def output(self, action, path, target, maximum, expected_digest, expected_size):
        ensure(
            isinstance(path, str)
            and path.startswith(f"/v1/actions/{action}/outputs/")
            and ".." not in path,
            "INVALID_BINDING",
            "Remote output path is outside the action",
        )
        parsed = urlsplit(urljoin(str(self.client.base_url), path))
        ensure(
            (parsed.scheme, parsed.hostname, parsed.port) == self.origin,
            "INVALID_BINDING",
            "Remote output changed origin",
        )
        target = Path(target)
        if target.exists():
            content = target.read_bytes()
            ensure(
                len(content) == expected_size
                and "sha256:" + hashlib.sha256(content).hexdigest() == expected_digest,
                "INVALID_BINDING",
                "Previously downloaded remote output changed",
            )
            return "application/json"
        temporary = target.with_suffix(target.suffix + ".download")
        try:
            with self.client.stream("GET", path) as response:
                if 300 <= response.status_code < 400:
                    raise ServiceFault("INVALID_BINDING", "Remote redirects are forbidden", 502)
                if response.status_code == 404:
                    return None
                if response.status_code >= 400:
                    raise ServiceFault(
                        "DEPENDENCY_UNAVAILABLE",
                        "Remote output request failed",
                        response.status_code,
                    )
                claimed_length = response.headers.get("content-length")
                ensure(
                    claimed_length is None or int(claimed_length) <= maximum,
                    "INVALID_BINDING",
                    "Remote output exceeds limit",
                )
                total, hasher = 0, hashlib.sha256()
                with temporary.open("wb") as stream:
                    for chunk in response.iter_bytes(1024 * 1024):
                        total += len(chunk)
                        ensure(total <= maximum, "INVALID_BINDING", "Remote output exceeds limit")
                        stream.write(chunk)
                        hasher.update(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                ensure(
                    total == expected_size and "sha256:" + hasher.hexdigest() == expected_digest,
                    "INVALID_BINDING",
                    "Remote output digest differs",
                )
                os.replace(temporary, target)
                target.chmod(0o444)
                return response.headers.get("content-type", "")
        except httpx.HTTPError as exc:
            raise OSError("Remote communication failed") from exc
        finally:
            temporary.unlink(missing_ok=True)

    def close(self):
        self.client.close()
