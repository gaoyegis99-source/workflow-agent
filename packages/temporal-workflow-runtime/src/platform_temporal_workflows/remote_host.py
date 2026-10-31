"""Fixed-endpoint remote plugin host for HTTP, MCP gateways, and compute runners.

This adapter lets the Temporal Worker call a reviewed remote plugin host without
granting it platform-database access.  The remote host may itself be an HTTP,
MCP, CWL/Toil, or container-execution service; the workflow can never choose
its URL, credential, or protocol operation.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib import error, parse, request

from .governance import GovernanceError, PluginExecutionPort
from .models import (
    ActionCommand,
    NodeOutcome,
    PluginExecutionMode,
    PluginManifest,
    PluginReference,
    WorkflowContractError,
    canonical_json,
)
from .observability import plugin_span

_MAX_PROTOCOL_BYTES = 1_048_576
_PROTOCOL_VERSION = "v7-remote-plugin-v1"
_SENSITIVE_HEADER_NAMES = frozenset({"authorization", "cookie", "proxy-authorization", "x-api-key"})


class RemotePluginError(GovernanceError):
    """A remote plugin response was absent, malformed, or not confirmed."""


def _no_redirect_opener() -> request.OpenerDirector:
    """Create the HTTP handler only in an Activity/worker process.

    Temporal imports this package while validating sandboxed Workflow code.
    Defining a subclass of ``urllib`` there is itself a prohibited I/O-module
    operation, even though the handler is never used by Workflow code.
    """

    class NoRedirect(request.HTTPRedirectHandler):
        def redirect_request(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

    return request.build_opener(NoRedirect())


@dataclass(frozen=True)
class RemotePluginDefinition:
    """One exact plugin's immutable remote endpoints and credential binding."""

    manifest: PluginManifest
    invoke_url: str
    reconcile_url: str
    credential_file: Path | None = None
    headers: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, PluginManifest):
            raise WorkflowContractError("remote plugin requires an exact manifest")
        if self.manifest.execution_mode is PluginExecutionMode.PROCESS:
            raise WorkflowContractError("process-mode plugins must use a process host")
        invoke = _remote_url(self.invoke_url, "remote plugin invoke_url")
        reconcile = _remote_url(self.reconcile_url, "remote plugin reconcile_url")
        if (invoke.scheme, invoke.netloc) != (reconcile.scheme, reconcile.netloc):
            raise WorkflowContractError(
                "remote plugin reconcile_url must use the same registered origin"
            )
        if (self.manifest.credential_ref is None) != (self.credential_file is None):
            raise WorkflowContractError(
                "remote plugin credential file must exactly match its credential reference"
            )
        if self.credential_file is not None:
            object.__setattr__(self, "credential_file", Path(self.credential_file))
        headers = dict(self.headers)
        for name, value in headers.items():
            if (
                not isinstance(name, str)
                or not name
                or any(character in name for character in "\r\n:")
                or name.lower() in _SENSITIVE_HEADER_NAMES
                or not isinstance(value, str)
                or "\r" in value
                or "\n" in value
            ):
                raise WorkflowContractError("remote plugin headers are invalid or sensitive")
        object.__setattr__(self, "headers", headers)


class RemotePluginHost(PluginExecutionPort):
    """Call fixed remote endpoints with the stable V7 action identity.

    ``invoke`` and ``reconcile`` URLs are boot-time registration data.  Only
    the full reviewed manifest can select an entry.  A transport failure is
    deliberately an unknown result: the Activity returns to the ledger-backed
    reconciliation path instead of retrying the external call as a new action.
    """

    def __init__(self) -> None:
        self._definitions: dict[tuple[str, str, str], RemotePluginDefinition] = {}
        self._opener = _no_redirect_opener()

    def register(self, definition: RemotePluginDefinition) -> None:
        key = _plugin_key(definition.manifest.plugin)
        if key in self._definitions:
            raise GovernanceError("remote plugin is already registered for this exact version")
        self._definitions[key] = definition

    async def invoke(self, command: ActionCommand, manifest: PluginManifest) -> NodeOutcome:
        return await self._call("invoke", command, manifest)

    async def reconcile(
        self, command: ActionCommand, manifest: PluginManifest
    ) -> NodeOutcome | None:
        return await self._call("reconcile", command, manifest)

    async def _call(
        self, operation: str, command: ActionCommand, manifest: PluginManifest
    ) -> NodeOutcome | None:
        definition = self._definition(manifest)
        _same_manifest(command.plugin, definition.manifest.plugin)
        url = definition.invoke_url if operation == "invoke" else definition.reconcile_url
        body = canonical_json(
            {
                "schema_version": _PROTOCOL_VERSION,
                "operation": operation,
                "action": command.to_mapping(),
                "manifest": definition.manifest.to_mapping(),
            }
        ).encode("utf-8")
        if len(body) > _MAX_PROTOCOL_BYTES:
            raise RemotePluginError("remote plugin request exceeds the protocol limit")
        with plugin_span(operation, command, definition.manifest):
            raw = await asyncio.to_thread(self._request, url, command, definition, body)
        if len(raw) > _MAX_PROTOCOL_BYTES:
            raise RemotePluginError("remote plugin response exceeds the protocol limit")
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RemotePluginError("remote plugin returned malformed protocol JSON") from error
        if operation == "reconcile" and decoded is None:
            return None
        if not isinstance(decoded, Mapping):
            raise RemotePluginError("remote plugin result must be an outcome object")
        try:
            return NodeOutcome.from_mapping(decoded)
        except WorkflowContractError as error:
            raise RemotePluginError("remote plugin returned an invalid outcome") from error

    def _request(
        self,
        url: str,
        command: ActionCommand,
        definition: RemotePluginDefinition,
        body: bytes,
    ) -> bytes:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Idempotency-Key": command.idempotency_key,
            "X-V7-Action-ID": command.action_id,
            **definition.headers,
        }
        credential_file = definition.credential_file
        if credential_file is not None:
            headers["Authorization"] = "Bearer " + _credential(credential_file)
        outbound = request.Request(url, data=body, headers=headers, method="POST")
        try:
            with self._opener.open(
                outbound, timeout=definition.manifest.timeout_seconds
            ) as response:
                if not 200 <= response.status < 300:
                    raise RemotePluginError("remote plugin did not confirm the action outcome")
                return response.read(_MAX_PROTOCOL_BYTES + 1)
        except RemotePluginError:
            raise
        except error.HTTPError as response:
            # An HTTP error can occur after the remote host passed work to an
            # external system.  Ignore its body and reconcile the original id.
            response.close()
            raise RemotePluginError(
                "remote plugin did not confirm the action outcome"
            ) from response
        except (OSError, ValueError) as error_value:
            raise RemotePluginError("remote plugin is unavailable") from error_value

    def _definition(self, manifest: PluginManifest) -> RemotePluginDefinition:
        definition = self._definitions.get(_plugin_key(manifest.plugin))
        if definition is None:
            raise GovernanceError("remote plugin is not registered")
        if definition.manifest.to_mapping() != manifest.to_mapping():
            raise GovernanceError("remote plugin manifest differs from the frozen definition")
        return definition


def _remote_url(value: object, label: str) -> parse.SplitResult:
    if not isinstance(value, str):
        raise WorkflowContractError(f"{label} must be an HTTPS URL")
    parsed = parse.urlsplit(value)
    local_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.query
        or (parsed.scheme != "https" and not local_http)
    ):
        raise WorkflowContractError(f"{label} must be an HTTPS URL")
    return parsed


def _credential(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise RemotePluginError("remote plugin credential file is unavailable or unsafe")
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError as error_value:
        raise RemotePluginError("could not read remote plugin credential file") from error_value
    if not value or "\r" in value or "\n" in value:
        raise RemotePluginError("remote plugin credential file is invalid")
    return value


def _plugin_key(reference: PluginReference) -> tuple[str, str, str]:
    return reference.plugin_id, reference.version, reference.package_digest


def _same_manifest(first: PluginReference, second: PluginReference) -> None:
    if first != second:
        raise GovernanceError("action does not match the registered remote plugin")
