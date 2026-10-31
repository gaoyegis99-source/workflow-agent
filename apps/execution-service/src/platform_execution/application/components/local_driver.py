"""Local container execution and recovery driver."""

from datetime import UTC, datetime

from platform_contracts.service import ServiceFault, ensure

from platform_execution.application.execution_state import (
    LOCAL_EXECUTION_STATE,
)

from .collection import OutputCollector
from .container_diagnostics import classify_container_failure
from .finalization import ExecutionLifecycle
from .materialization import InputMaterializer
from .protocols import ActionComponent, ActionServices
from .publication import ArtifactPublisher


class LocalExecutionDriver(ActionComponent):
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

    def run(self, row, root):
        info = self.docker.inspect(self.docker.name(row))
        if row["cancel_requested"]:
            return self.lifecycle.cancel(row, info)
        phase = LOCAL_EXECUTION_STATE.phase(row)
        if phase == "ACCEPTED":
            if "image_id" not in row["runtime"]:
                profile = self.docker.profile(row)
                if not self.execution.change(
                    row, image_id=profile["image_id"], implementation_profile=profile
                ):
                    return
            return self.input_materializer.run(row, root)
        if phase in ("PREPARED", "CREATE_INTENT", "CREATED"):
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
                        error=permit.get("reason", "PERMIT_DENIED"),
                    )
                return (
                    self.lifecycle.cancel(row, info)
                    if info
                    else self.lifecycle.abort(row, "REJECTED", "PERMIT_DENIED")
                )
            if phase != "CREATED":
                if not self.execution.transition(row, "CREATE_INTENT"):
                    return
                if info is None:
                    info = self.docker.create(row, root)
                self.docker.check(row, info)
                self.execution.trace(
                    row,
                    "CONTAINER_CREATED",
                    info["Id"],
                    attributes={"container_id": info["Id"]},
                )
                return self.execution.transition(row, "CREATED", container_id=info["Id"])
            ensure(info is not None, "INVALID_BINDING", "Created container disappeared")
            self.docker.check(row, info)
            if not self.execution.transition(row, "START_INTENT"):
                return
            self.docker.start(info["Id"])
            info = self.docker.inspect(info["Id"])
            if info is not None and not info["State"]["StartedAt"].startswith("0001-"):
                self.execution.trace(
                    row,
                    "CONTAINER_STARTED",
                    info["Id"],
                    attributes={"container_id": info["Id"]},
                )
                return self.execution.transition(row, "RUNNING", state="RUNNING", executed=True)
            return
        if phase in ("START_INTENT", "RUNNING"):
            if info is None:
                # A vanished container after start intent is unknown, never "not executed".
                raise OSError("Container identity unavailable after start intent")
            self.docker.check(row, info)
            state = info["State"]
            if state["StartedAt"].startswith("0001-"):
                # Do not issue another start for an uncertain start intent. Remove the original
                # never-started container before a definite unexecuted failure can be recorded.
                # Persist the stop intent before deletion, so a crash or Data outage after
                # removing a never-started container cannot strand recovery on a missing ID.
                return self.execution.transition(row, "STOPPING_UNSTARTED")
            if not row["executed"]:
                if not self.execution.transition(row, phase, state="RUNNING", executed=True):
                    return
            if row["runtime"].get("uncertain") and not self.execution.transition(row, "RUNNING"):
                return
            if state["Running"]:
                started = datetime.fromisoformat(state["StartedAt"].replace("Z", "+00:00"))
                size = sum(f.stat().st_size for f in (root / "output").iterdir() if f.is_file())
                timeout = (
                    row["runtime"].get("implementation_profile", {}).get("timeout_seconds", 120)
                )
                if (
                    datetime.now(UTC) - started
                ).total_seconds() > timeout or size > 100 * 1024 * 1024:
                    if self.execution.change(row, error="RESOURCE_LIMIT"):
                        self.docker.stop(info["Id"])
                return
            # The rootless container UID needed a writable/listable bind
            # mount while running.  The host worker owns the directory and
            # closes that window before inspecting or publishing any output.
            (root / "output").chmod(0o700)
            self.execution.trace(
                row,
                "CONTAINER_EXITED",
                info["Id"],
                attributes={"container_id": info["Id"], "exit_code": state["ExitCode"]},
            )
            if state["ExitCode"] != 0 or row["runtime"].get("error"):
                failure = classify_container_failure(row, root, state)
                return self.lifecycle.abort(row, "FAILED", failure.error, **failure.details)
            try:
                return self.output_collector.run(row, root)
            except (ValueError, KeyError, FileNotFoundError, ServiceFault) as exc:
                return self.lifecycle.abort(row, "FAILED", "INVALID_OUTPUT_" + type(exc).__name__)
        if phase == "STOPPING_UNSTARTED":
            if info:
                self.docker.check(row, info)
                if not info["State"]["StartedAt"].startswith("0001-"):
                    return self.execution.transition(row, "RUNNING", state="RUNNING", executed=True)
                self.docker.stop(info["Id"], never_started=True)
                return
            return self.lifecycle.abort(row, "FAILED", "START_INTERRUPTED")
        if phase == "PUBLISHING":
            return self.artifact_publisher.run(row, root)
        raise ValueError("Unknown execution phase")
