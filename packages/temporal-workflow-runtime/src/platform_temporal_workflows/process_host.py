"""A fail-closed, process-isolated V7 plugin host.

The Temporal Worker owns the governance ledger and invokes only reviewed
``ProcessPluginDefinition`` values configured at boot.  A plugin receives one
credential-free JSON request on stdin and returns one JSON outcome on stdout;
it never receives a database connection, ledger object, or arbitrary command
line assembled from a workflow input.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

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

_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_SENSITIVE_ENVIRONMENT_NAME = re.compile(
    r"(?:API_KEY|AUTHORIZATION|PASSWORD|PRIVATE_KEY|SECRET|TOKEN)$", re.IGNORECASE
)
_RESERVED_ENVIRONMENT_NAMES = frozenset(
    {"LANG", "LC_ALL", "PATH", "V7_PLUGIN_PROTOCOL", "V7_PLUGIN_CREDENTIAL_FILE"}
)
_MAX_PROTOCOL_BYTES = 1_048_576
_PROTOCOL_VERSION = "v7-process-plugin-v1"


class ProcessPluginError(GovernanceError):
    """A process plugin failed after its external effect may have begun."""


@dataclass(frozen=True)
class ProcessPluginDefinition:
    """One exact reviewed plugin executable and its non-secret launch policy."""

    manifest: PluginManifest
    command: tuple[str, ...]
    environment: Mapping[str, str] = field(default_factory=dict)
    credential_file: Path | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.manifest, PluginManifest):
            raise WorkflowContractError("process plugin requires an exact manifest")
        if self.manifest.execution_mode is not PluginExecutionMode.PROCESS:
            raise WorkflowContractError("process host only accepts process-mode plugins")
        command = tuple(self.command)
        if not command or any(
            not isinstance(item, str) or not item or "\x00" in item for item in command
        ):
            raise WorkflowContractError("process plugin command is invalid")
        executable = Path(command[0])
        if not executable.is_absolute():
            raise WorkflowContractError("process plugin executable must be an absolute path")
        object.__setattr__(self, "command", command)
        environment = dict(self.environment)
        for name, value in environment.items():
            if (
                not isinstance(name, str)
                or _ENVIRONMENT_NAME.fullmatch(name) is None
                or name in _RESERVED_ENVIRONMENT_NAMES
                or _SENSITIVE_ENVIRONMENT_NAME.search(name) is not None
                or not isinstance(value, str)
                or "\x00" in value
            ):
                raise WorkflowContractError("process plugin environment is invalid or sensitive")
        object.__setattr__(self, "environment", environment)
        if (self.manifest.credential_ref is None) != (self.credential_file is None):
            raise WorkflowContractError(
                "process plugin credential file must exactly match its credential reference"
            )
        if self.credential_file is not None:
            object.__setattr__(self, "credential_file", Path(self.credential_file))


class ProcessPluginHost(PluginExecutionPort):
    """Launch reviewed plugins in one fresh, constrained-environment process.

    The host deliberately has no generic ``run(command)`` API.  The only
    command it can execute is one registered against the exact manifest used by
    a frozen :class:`ActionCommand`.  A child process is short-lived, has an
    allowlisted environment, and communicates using bounded JSON messages.
    """

    def __init__(self) -> None:
        self._definitions: dict[tuple[str, str, str], ProcessPluginDefinition] = {}

    def register(self, definition: ProcessPluginDefinition) -> None:
        key = _plugin_key(definition.manifest.plugin)
        if key in self._definitions:
            raise GovernanceError("process plugin is already registered for this exact version")
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
        request = canonical_json(
            {
                "schema_version": _PROTOCOL_VERSION,
                "operation": operation,
                "action": command.to_mapping(),
                "manifest": definition.manifest.to_mapping(),
            }
        ).encode("utf-8")
        if len(request) > _MAX_PROTOCOL_BYTES:
            raise ProcessPluginError("plugin request exceeds the protocol limit")
        with plugin_span(operation, command, definition.manifest):
            stdout, return_code = await self._run(definition, request)
        if return_code != 0:
            # A plugin may have issued an external operation before exiting;
            # hand that case to the reconciler rather than treating it as a
            # known business failure.
            raise ProcessPluginError("plugin process exited without a confirmed outcome")
        if len(stdout) > _MAX_PROTOCOL_BYTES:
            raise ProcessPluginError("plugin response exceeds the protocol limit")
        try:
            decoded = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ProcessPluginError("plugin returned malformed protocol JSON") from error
        if operation == "reconcile" and decoded is None:
            return None
        if not isinstance(decoded, Mapping):
            raise ProcessPluginError("plugin result must be an outcome object")
        try:
            return NodeOutcome.from_mapping(decoded)
        except WorkflowContractError as error:
            raise ProcessPluginError("plugin returned an invalid outcome") from error

    async def _run(self, definition: ProcessPluginDefinition, request: bytes) -> tuple[bytes, int]:
        environment = _environment(definition)
        try:
            process = await asyncio.create_subprocess_exec(
                *definition.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=environment,
                limit=_MAX_PROTOCOL_BYTES,
            )
        except OSError as error:
            raise ProcessPluginError("could not start reviewed plugin process") from error
        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(request), timeout=definition.manifest.timeout_seconds
            )
        except TimeoutError as error:
            process.kill()
            await process.communicate()
            raise ProcessPluginError("plugin process timed out") from error
        return stdout, int(process.returncode or 0)

    def _definition(self, manifest: PluginManifest) -> ProcessPluginDefinition:
        definition = self._definitions.get(_plugin_key(manifest.plugin))
        if definition is None:
            raise GovernanceError("process plugin is not registered")
        if definition.manifest.to_mapping() != manifest.to_mapping():
            raise GovernanceError("process plugin manifest differs from the frozen definition")
        return definition


def _environment(definition: ProcessPluginDefinition) -> dict[str, str]:
    """Pass only a small, explicitly non-secret environment into the child."""

    environment = {
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.defpath,
        "V7_PLUGIN_PROTOCOL": _PROTOCOL_VERSION,
    }
    environment.update(definition.environment)
    credential_file = definition.credential_file
    if credential_file is not None:
        if credential_file.is_symlink() or not credential_file.is_file():
            raise ProcessPluginError("plugin credential file is unavailable or unsafe")
        environment["V7_PLUGIN_CREDENTIAL_FILE"] = str(credential_file.resolve())
    return environment


def _plugin_key(reference: PluginReference) -> tuple[str, str, str]:
    return reference.plugin_id, reference.version, reference.package_digest


def _same_manifest(first: PluginReference, second: PluginReference) -> None:
    if first != second:
        raise GovernanceError("action does not match the registered process plugin")
