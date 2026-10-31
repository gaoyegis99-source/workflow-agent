"""Remote execution submission, observation, and recovery driver."""

import hashlib
import os
import re
import shutil

from platform_contracts.service import ServiceFault, ensure

from platform_execution.application.execution_state import (
    LOCAL_EXECUTION_STATE,
)

from .collection import OutputCollector
from .finalization import ExecutionLifecycle
from .materialization import InputMaterializer
from .protocols import ActionComponent, ActionServices
from .publication import ArtifactPublisher


class RemoteExecutionDriver(ActionComponent):
    def __init__(
        self,
        services: ActionServices,
        *,
        input_materializer: InputMaterializer,
        output_collector: OutputCollector,
        artifact_publisher: ArtifactPublisher,
        lifecycle: ExecutionLifecycle,
    ):
        super().__init__(services)
        self.input_materializer = input_materializer
        self.output_collector = output_collector
        self.artifact_publisher = artifact_publisher
        self.lifecycle = lifecycle

    @staticmethod
    def remote_execution(row):
        return (
            row.get("payload", {}).get("target", {}).get("execution_binding", {}).get("kind")
            == "REMOTE"
        )

    def remote_client(self, row):
        ensure(
            self.remote_factory is not None,
            "DEPENDENCY_UNAVAILABLE",
            "Remote runtime unavailable",
            503,
        )
        return self.execution.guard(
            "remote", self.remote_factory(row["payload"]["_remote_binding"])
        )

    def apply_remote_observation(self, row, call, observation):
        ensure(isinstance(observation, dict), "INVALID_BINDING", "Invalid remote observation")
        ensure(
            observation.get("action_id") == row["action_id"],
            "INVALID_BINDING",
            "Remote action identity changed",
        )
        revision = observation.get("revision")
        state = observation.get("state")
        executed = observation.get("executed")
        remote_id = observation.get("remote_execution_id")
        event_id = observation.get("event_id")
        ensure(
            type(revision) is int
            and revision >= 1
            and state
            in {
                "ACCEPTED",
                "QUEUED",
                "STARTING",
                "RUNNING",
                "COLLECTING",
                "SUCCEEDED",
                "FAILED",
                "REJECTED",
                "CANCELLED",
            }
            and type(executed) is bool
            and isinstance(remote_id, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", remote_id)
            and isinstance(event_id, str)
            and 0 < len(event_id) <= 256,
            "INVALID_BINDING",
            "Invalid remote fact",
        )
        if revision < call["remote_revision"]:
            return call
        ensure(
            (state not in {"RUNNING", "COLLECTING", "SUCCEEDED", "FAILED"} or executed)
            and (state not in {"ACCEPTED", "QUEUED", "STARTING"} or not executed)
            and (state != "REJECTED" or not executed),
            "INVALID_BINDING",
            "Remote state and executed fact conflict",
        )
        ensure(
            not call["remote_execution_id"] or call["remote_execution_id"] == remote_id,
            "INVALID_BINDING",
            "Remote execution identity changed",
        )
        ensure(
            not call["executed"] or executed, "INVALID_BINDING", "Remote executed fact regressed"
        )
        protocol = call.get("binding", {}).get("protocol", "v7-remote-execution-v1")
        if protocol == "v7-remote-execution-v2":
            ensure(
                observation.get("implementation_digest")
                == call["binding"]["target"].get("implementation_digest")
                and observation.get("cleanup_state")
                in {"NOT_REQUIRED", "PENDING", "CONFIRMED", "UNKNOWN"}
                and (
                    observation.get("started_at") is None
                    or isinstance(observation.get("started_at"), str)
                )
                and (
                    observation.get("finished_at") is None
                    or isinstance(observation.get("finished_at"), str)
                ),
                "INVALID_BINDING",
                "Remote v2 execution fact differs",
            )
        if revision == call["remote_revision"] and call["observation"] is not None:
            ensure(
                call["observation"] == observation,
                "INVALID_BINDING",
                "Conflicting remote observation revision",
            )
        elif revision > call["remote_revision"]:
            ensure(
                self.repo.observe_remote(
                    row,
                    observation,
                    remote_execution_id=remote_id,
                    remote_revision=revision,
                    remote_state=state,
                    executed=executed,
                    observation=observation,
                    phase="TRACKING",
                ),
                "DEPENDENCY_UNAVAILABLE",
                "Remote lease expired",
                503,
            )
            call = self.repo.remote(row["action_id"])
        if executed and not row["executed"]:
            if not self.execution.transition(
                row, LOCAL_EXECUTION_STATE.phase(row), executed=True, state="RUNNING"
            ):
                return call
        remote = row["runtime"].get("remote", {}) | {
            "state": state,
            "revision": revision,
            "remote_execution_id": remote_id,
            "executed": executed,
        }
        self.execution.transition(row, "REMOTE_TRACKING", remote=remote)
        return call

    def cancel_remote(self, row):
        client = self.remote_client(row)
        try:
            observation = client.cancel(row["action_id"])
            if observation is None:
                raise OSError("Remote cancellation is unknown")
            call = self.apply_remote_observation(
                row, self.repo.remote(row["action_id"]), observation
            )
            state = call["remote_state"]
            if state in {"CANCELLED", "REJECTED", "FAILED", "SUCCEEDED"}:
                return self.lifecycle.abort(row, "CANCELLED", "REMOTE_" + state)
        finally:
            client.close()

    def remote_diagnostic(self, row, category, code):
        remote = row["runtime"].get("remote", {}) | {
            "failure_category": category,
            "failure_code": code,
        }
        self.execution.change(row, remote=remote)

    @staticmethod
    def _remote_output_matches(directory, expected, outputs):
        if not directory.is_dir() or directory.is_symlink():
            return False
        paths = list(directory.iterdir())
        if {path.name for path in paths} != set(expected.values()):
            return False
        for port, filename in expected.items():
            path, meta = directory / filename, outputs[port]
            if not path.is_file() or path.is_symlink():
                return False
            content = path.read_bytes()
            if len(content) != meta["size"]:
                return False
            if "sha256:" + hashlib.sha256(content).hexdigest() != meta["digest"]:
                return False
        return True

    def collect_remote_outputs(self, row, root, call, client):
        outputs = call["observation"].get("outputs")
        expected = row["runtime"]["implementation_profile"]["outputs"]
        ensure(
            isinstance(outputs, dict) and set(outputs) == set(expected),
            "INVALID_BINDING",
            "Remote output set differs",
        )
        for port in expected:
            meta = outputs[port]
            ensure(
                isinstance(meta, dict)
                and isinstance(meta.get("path"), str)
                and isinstance(meta.get("digest"), str)
                and re.fullmatch(r"sha256:[0-9a-f]{64}", meta["digest"])
                and type(meta.get("size")) is int,
                "INVALID_BINDING",
                "Invalid remote output metadata",
            )

        output = root / "output"
        if self._remote_output_matches(output, expected, outputs):
            self.repo.change_remote(row, phase="OUTPUT_VERIFIED")
            return self.output_collector.run(row, root)

        incoming_root = root / "remote-incoming"
        incoming = incoming_root / str(call["remote_revision"])
        incoming.mkdir(parents=True, exist_ok=True, mode=0o700)
        if output.is_dir() and any(output.iterdir()):
            quarantine = incoming_root / "legacy-output"
            ensure(
                not quarantine.exists(),
                "INVALID_BINDING",
                "Multiple unverified remote output sets exist",
            )
            os.replace(output, quarantine)
            output.mkdir(mode=0o700)
        total = 0
        for port, filename in expected.items():
            meta = outputs[port]
            remaining = call["binding"]["limits"]["output_bytes"] - total
            ensure(
                0 <= meta["size"] <= remaining,
                "INVALID_BINDING",
                "Remote output exceeds declared limit",
            )
            content_type = client.output(
                row["action_id"],
                meta["path"],
                incoming / filename,
                remaining,
                meta["digest"],
                meta["size"],
            )
            if content_type is None:
                self.remote_diagnostic(row, "COMMUNICATION", "REMOTE_OUTPUT_NOT_VISIBLE")
                raise OSError("Remote output is temporarily not visible")
            ensure(
                meta.get("media_type") == "application/json"
                and content_type.split(";", 1)[0] == "application/json",
                "INVALID_BINDING",
                "Remote output media type differs",
            )
            total += meta["size"]
        ensure(
            total <= call["binding"]["limits"]["output_bytes"]
            and self._remote_output_matches(incoming, expected, outputs),
            "INVALID_BINDING",
            "Remote output set is incomplete",
        )
        directory = os.open(incoming, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        ensure(
            output.is_dir() and not output.is_symlink() and not any(output.iterdir()),
            "INVALID_BINDING",
            "Private output directory is not empty",
        )
        output.rmdir()
        os.replace(incoming, output)
        parent = os.open(root, os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
        shutil.rmtree(incoming_root, ignore_errors=True)
        self.repo.change_remote(row, phase="OUTPUT_VERIFIED")
        return self.output_collector.run(row, root)

    def run(self, row, root):
        phase = LOCAL_EXECUTION_STATE.phase(row)
        if phase == "PUBLISHING":
            return self.artifact_publisher.run(row, root)
        if row["cancel_requested"]:
            return self.cancel_remote(row)
        if phase == "ACCEPTED":
            return self.input_materializer.run(row, root)
        call = self.repo.remote(row["action_id"])
        client = self.remote_client(row)
        try:
            if phase == "PREPARED":
                permit = self.execution.permit(row)
                if permit["decision"] == "WAIT":
                    return
                if permit["decision"] != "ALLOW":
                    # Platform can reject a workflow node before assigning a compute
                    # slot (for example when its frozen service binding was disabled).
                    # In that case the node already has a terminal Platform fact and
                    # must not enter REPORTING: observations for nodes without a slot
                    # are deliberately rejected by Platform.
                    if row.get("parent_execution_id") and not permit.get("reportable", True):
                        return self.execution.transition(
                            row,
                            "FINAL",
                            state="REJECTED",
                            publication="PRIVATE",
                            error=permit.get("reason", "PERMIT_DENIED"),
                        )
                    return self.lifecycle.abort(row, "REJECTED", "PERMIT_DENIED")
                content = (root / "input/vector.gpkg").read_bytes()
                digest_value = row["payload"]["bindings"]["vector"]["representation"]["digest"]
                ensure(
                    len(content) <= call["binding"]["limits"]["input_bytes"],
                    "INVALID_BINDING",
                    "Remote input exceeds binding limit",
                )
                response = client.upload(row["action_id"], "vector", content, digest_value)
                ensure(
                    response is not None,
                    "DEPENDENCY_UNAVAILABLE",
                    "Remote input status unknown",
                    503,
                )
                ensure(
                    self.repo.change_remote(row, phase="SUBMITTING"),
                    "DEPENDENCY_UNAVAILABLE",
                    "Remote lease expired",
                    503,
                )
                return self.execution.transition(row, "REMOTE_SUBMITTING")
            if phase == "REMOTE_SUBMITTING":
                permit = self.execution.permit(row)
                if permit["decision"] == "WAIT":
                    return
                if permit["decision"] != "ALLOW":
                    if row.get("parent_execution_id") and not permit.get("reportable", True):
                        return self.execution.transition(
                            row,
                            "FINAL",
                            state="REJECTED",
                            publication="PRIVATE",
                            error=permit.get("reason", "PERMIT_DENIED_BEFORE_REMOTE_SEND"),
                        )
                    return self.lifecycle.abort(row, "REJECTED", "PERMIT_DENIED_BEFORE_REMOTE_SEND")
                request = {
                    "schema_version": (
                        2 if call["binding"]["protocol"] == "v7-remote-execution-v2" else 1
                    ),
                    "action_id": row["action_id"],
                    "request_digest": call["request_digest"],
                    "target": call["binding"]["target"],
                    "parameters": row["payload"]["parameters"],
                    "inputs": {
                        "vector": {
                            "digest": row["payload"]["bindings"]["vector"]["representation"][
                                "digest"
                            ]
                        }
                    },
                    "limits": call["binding"]["limits"],
                }
                observation = client.submit(row["action_id"], request)
                if observation is None:
                    raise OSError("Remote submit response is unknown")
                self.apply_remote_observation(row, call, observation)
                return
            if phase == "REMOTE_TRACKING":
                observation = client.query(row["action_id"])
                if observation is None:
                    self.remote_diagnostic(row, "COMMUNICATION", "REMOTE_NOT_VISIBLE")
                    raise OSError("Remote action is temporarily not found")
                call = self.apply_remote_observation(row, call, observation)
                state = call["remote_state"]
                if state == "SUCCEEDED":
                    try:
                        return self.collect_remote_outputs(row, root, call, client)
                    except ServiceFault as problem:
                        if problem.status < 500:
                            self.remote_diagnostic(
                                row, "REMOTE_PROTOCOL", "REMOTE_PROTOCOL_VIOLATION"
                            )
                        raise
                if state in {"FAILED", "REJECTED", "CANCELLED"}:
                    self.remote_diagnostic(
                        row,
                        "REMOTE_REJECTION"
                        if state in {"REJECTED", "CANCELLED"} and not call["executed"]
                        else "REMOTE_EXECUTION",
                        "REMOTE_" + state,
                    )
                    terminal = (
                        "REJECTED"
                        if state == "REJECTED" and not call["executed"]
                        else ("CANCELLED" if state == "CANCELLED" else "FAILED")
                    )
                    return self.lifecycle.abort(row, terminal, "REMOTE_" + state)
                return
        finally:
            client.close()
