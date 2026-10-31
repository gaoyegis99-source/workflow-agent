"""Workflow engine execution and recovery driver."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from platform_contracts.engine_runtime import validate as validate_runtime_profile
from platform_contracts.service import ensure

from platform_execution.application.execution_state import (
    WORKFLOW_EXECUTION_STATE,
)
from platform_execution.engine_package import validate as validate_engine_package

from .collection import WorkflowOutputCollector
from .finalization import WorkflowLifecycle
from .materialization import WorkflowInputMaterializer
from .protocols import WorkflowComponent, WorkflowServices
from .publication import WorkflowArtifactPublisher


class WorkflowExecutionDriver(WorkflowComponent):
    def __init__(
        self,
        services: WorkflowServices,
        *,
        input_materializer: WorkflowInputMaterializer,
        output_collector: WorkflowOutputCollector,
        artifact_publisher: WorkflowArtifactPublisher,
        lifecycle: WorkflowLifecycle,
    ):
        super().__init__(services)
        self.input_materializer = input_materializer
        self.output_collector = output_collector
        self.artifact_publisher = artifact_publisher
        self.lifecycle = lifecycle

    @staticmethod
    def engine_profile(row):
        value = row["runtime"].get("_engine_runtime_profile")
        ensure(value is not None, "INVALID_BINDING", "Workflow engine profile is missing", 422)
        return validate_runtime_profile(value)

    def configure(self, row, root, epoch, restart):
        engine_package = validate_engine_package(row["payload"])
        config = {
            "compiled": row["payload"]["compiled"],
            "execution_id": row["execution_id"],
            "execution_url": self.execution_url,
            "token": (root / "bridge.token").read_text(),
            "epoch": epoch,
            "restart": restart,
            "engine_runtime_profile": self.engine_profile(row),
            # The persisted field name is part of the existing execution
            # receipt contract; direct Compose stores its deployment revision
            # here without consulting a Release Lock.
            "release_digest": self.runtime_config.deployment.deployment_revision,
        }
        config["engine_package"] = engine_package
        path = root / f"config-{epoch}.json"
        # A rootless daemon maps the explicitly non-root engine UID to a
        # subordinate host UID.  The file is mounted read-only and remains
        # behind the worker-owned 0700 work root, so 0444 grants the engine
        # access without exposing it to other host users.
        self.execution.save(path, json.dumps(config, sort_keys=True).encode(), 0o444)
        return path

    @staticmethod
    def expose_runtime(root):
        """Let the remapped engine UID write only its private bind root."""

        root.chmod(0o777)

    @staticmethod
    def seal_runtime(root):
        """Restore worker-only traversal after the engine has stopped."""

        root.chmod(0o700)

    @staticmethod
    def restartable_jobstore(root):
        """Recognize the pinned Toil file JobStore only after its root is durable."""
        return (root / "jobstore" / "shared" / "rootJobStoreID").is_file()

    def maintenance_restart(self, row, root, epoch):
        """Authorize exactly the epoch recorded by a verified P8 park manifest."""

        path = self.runtime_config.park_manifest_path
        if path is None or not path.is_file():
            return False
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
            supplied = value.pop("manifest_digest")
            payload = json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
            if supplied != "sha256:" + hashlib.sha256(payload).hexdigest():
                return False
            if value.get("schema_version") != 1 or value.get("state") != "PARKED":
                return False
            record = next(
                item
                for item in value.get("jobstores", [])
                if item.get("action_id") == row["action_id"]
                and item.get("execution_id") == row["execution_id"]
                and item.get("engine_epoch") == epoch
            )
            if Path(record["jobstore"]).resolve() != (Path(root) / "jobstore").resolve():
                return False
            for item in record.get("key_files", []):
                current = Path(record["jobstore"]) / item["path"]
                if (
                    current.stat().st_size != item["size"]
                    or "sha256:" + hashlib.sha256(current.read_bytes()).hexdigest()
                    != item["digest"]
                ):
                    return False
            return bool(record.get("restartable"))
        except (KeyError, OSError, StopIteration, TypeError, ValueError, json.JSONDecodeError):
            return False

    @staticmethod
    def exit_fact(root, epoch):
        """Read a complete exit fact for this epoch, including the legacy shape."""
        path = root / f"exit-{epoch}.json"
        try:
            value = json.loads(path.read_text())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if type(value) is not dict:
            return None
        if type(value.get("epoch")) is not int or value["epoch"] != epoch:
            return None
        if type(value.get("returncode")) is not int:
            return None
        if "schema_version" not in value:
            return value if set(value) == {"epoch", "returncode"} else None
        if (
            value.get("schema_version") != 1
            or set(value)
            != {
                "schema_version",
                "epoch",
                "returncode",
                "termination_signal",
                "forced",
            }
            or value.get("termination_signal") not in {None, "SIGTERM", "SIGINT"}
            or type(value.get("forced")) is not bool
        ):
            return None
        return value

    def run(self, row, root):
        if not self.repo.change(row):
            return
        if "payload" in row:
            validate_engine_package(row["payload"])
        phase = WORKFLOW_EXECUTION_STATE.phase(row)
        epoch = row["runtime"].get("engine_epoch", 0)
        info = self.docker.inspect(row["execution_id"], epoch) if epoch else None
        if row["cancel_requested"]:
            if row["runtime"].get("cancellation", {}).get("state") != "PENDING_REMOTE":
                pending = row["runtime"] | {"cancellation": {"state": "PENDING_REMOTE"}}
                if not self.repo.change(row, runtime=pending):
                    return
                row["runtime"] = pending
            if info and info["State"]["Running"]:
                self.docker.check(row, info, epoch)
                info = self.docker.stop(info["Id"])
                if info is not None and info["State"]["Running"]:
                    return
            self.seal_runtime(root)
            children = self.repo.cancel_children(row["execution_id"])
            if any(
                child["state"] not in {"SUCCEEDED", "FAILED", "CANCELLED", "REJECTED", "SEALED"}
                for child in children
            ):
                return
            # A queued cancellation and a permit denial are the same verified fact:
            # this workflow never started.  Keep the terminal observation stable
            # regardless of which notification wins the Platform/Execution race.
            state = "CANCELLED" if row["executed"] else "REJECTED"
            return self.lifecycle.abort(row, state, "CANCELLED")
        if phase == "ACCEPTED":
            return self.input_materializer.run(row, root)
        if phase in {"PREPARED", "RESTART_READY"}:
            permit = self.execution.permit(row)
            if permit["decision"] == "WAIT":
                return
            if permit["decision"] != "ALLOW":
                self.seal_runtime(root)
                return self.lifecycle.abort(row, "REJECTED", "PERMIT_DENIED")
            restart = phase == "RESTART_READY"
            epoch = epoch + 1
            ensure(
                not restart or self.restartable_jobstore(root),
                "INVALID_BINDING",
                "Recoverable JobStore root is missing",
            )
            config = self.configure(row, root, epoch, restart)
            self.expose_runtime(root)
            # ``docker create`` can time out after the daemon has committed the
            # container.  Reconcile the deterministic target-epoch identity on
            # every retry before issuing another create, just as the local
            # execution driver does for its persisted create intent.
            info = self.docker.inspect(row["execution_id"], epoch)
            if info is None:
                info = self.docker.create(row, root, epoch, config)
            self.docker.check(row, info, epoch)
            self.execution.trace(
                row,
                "ENGINE_CREATED",
                [epoch, info["Id"]],
                epoch=epoch,
                attributes={"container_id": info["Id"]},
            )
            return self.execution.transition(
                row, "ENGINE_CREATED", engine_epoch=epoch, container_id=info["Id"]
            )
        if phase == "ENGINE_CREATED":
            ensure(info is not None, "INVALID_BINDING", "Workflow engine disappeared before start")
            self.docker.check(row, info, epoch)
            self.expose_runtime(root)
            self.docker.start(info["Id"])
            self.execution.trace(
                row,
                "ENGINE_STARTED",
                [epoch, info["Id"]],
                epoch=epoch,
                attributes={"container_id": info["Id"]},
            )
            return self.execution.transition(row, "ENGINE_RUNNING", state="RUNNING", executed=True)
        if phase == "ENGINE_RUNNING":
            ensure(info is not None, "INVALID_BINDING", "Workflow engine identity unavailable")
            self.docker.check(row, info, epoch)
            if info["State"]["Running"]:
                self.expose_runtime(root)
                started = datetime.fromisoformat(info["State"]["StartedAt"].replace("Z", "+00:00"))
                ensure(
                    (datetime.now(UTC) - started).total_seconds() <= 3600,
                    "INVALID_BINDING",
                    "Workflow time limit exceeded",
                )
                return
            self.seal_runtime(root)
            self.execution.trace(
                row,
                "ENGINE_EXITED",
                [epoch, info["Id"]],
                epoch=epoch,
                attributes={
                    "container_id": info["Id"],
                    "exit_code": info["State"].get("ExitCode"),
                },
            )
            exit_fact = self.exit_fact(root, epoch)
            if exit_fact is None:
                if (
                    epoch == 1 or self.maintenance_restart(row, root, epoch)
                ) and self.restartable_jobstore(root):
                    return self.execution.transition(row, "RESTART_READY", first_exit="INTERRUPTED")
                return self.lifecycle.abort(row, "FAILED", "ENGINE_EXIT_UNKNOWN")
            code = exit_fact["returncode"]
            if code == 0:
                return self.output_collector.run(row, root)
            children = self.repo.children(row["execution_id"])
            if any(
                child["state"] in {"FAILED", "REJECTED", "CANCELLED", "SEALED"}
                for child in children
            ):
                return self.lifecycle.abort(row, "FAILED", "NODE_FAILED")
            if (
                epoch == 1 or self.maintenance_restart(row, root, epoch)
            ) and self.restartable_jobstore(root):
                return self.execution.transition(row, "RESTART_READY", first_exit=code)
            # A restarted Toil leader can retain the first leader's non-zero status even
            # after every platform node was recovered by its stable identity.  Accept that
            # recovery only when every observed child succeeded and the frozen output can
            # be independently collected and provenance-checked.  A missing/partial result
            # or any failed child still follows the failure path below.
            if children and all(child["state"] == "SUCCEEDED" for child in children):
                try:
                    return self.output_collector.run(row, root)
                except (ValueError, KeyError, FileNotFoundError):
                    pass
            return self.lifecycle.abort(row, "FAILED", "WORKFLOW_FAILED")
        if phase == "PUBLISHING":
            return self.artifact_publisher.run(row, root)
        raise ValueError("Unknown workflow phase")
