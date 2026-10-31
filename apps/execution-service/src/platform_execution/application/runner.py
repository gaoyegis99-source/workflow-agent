"""Execution worker coordinator; algorithms live in application.components."""

import logging
import os
import subprocess
from pathlib import Path
from uuid import uuid4

from platform_contracts.service import ServiceFault, ensure

from platform_execution.application.components import (
    ActionServices,
    ArtifactPublisher,
    ExecutionLifecycle,
    InputMaterializer,
    LocalExecutionDriver,
    OutputCollector,
    RemoteExecutionDriver,
)
from platform_execution.application.execution_lease import ExecutionLeaseGuard
from platform_execution.application.execution_policies import (
    ActionPermitPolicy,
    ActionTracePolicy,
)
from platform_execution.application.execution_runtime import ExecutionRuntime, LeaseLostError
from platform_execution.application.execution_state import LOCAL_EXECUTION_STATE


def execution_root_mode(row):
    """Keep a live rootless workflow mount writable across worker ticks."""

    if row.get("payload", {}).get("kind") == "WORKFLOW" and row.get("runtime", {}).get("phase") in {
        "ENGINE_CREATED",
        "ENGINE_RUNNING",
    }:
        return 0o777
    return 0o700


class Runner:
    def __init__(
        self,
        repository,
        root,
        docker,
        data,
        platform,
        workflow=None,
        work_classes=None,
        remote_factory=None,
        table_inspector=None,
    ):
        self.repo, self.root, self.docker = repository, Path(root).resolve(), docker
        self.data, self.platform, self.owner = data, platform, uuid4().hex
        self.workflow = workflow
        self.work_classes = work_classes
        self.remote_factory = remote_factory
        self.table_inspector = table_inspector
        self.lease_seconds, self.heartbeat_seconds = 30, 10
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            self.root.chmod(0o700)
        except PermissionError:
            # A direct-Compose bind root is owned by the rootless daemon's
            # namespace root, not by the non-root service process.  The
            # private instance parent protects the host path; require that the
            # prepared mount is writable and traversable before accepting it.
            if not os.access(self.root, os.R_OK | os.W_OK | os.X_OK):
                raise
        self.execution = ExecutionRuntime(
            repository,
            platform,
            trace_policy=ActionTracePolicy(),
            permit_policy=ActionPermitPolicy(),
            changed_message="Frozen input changed",
            permit_source="EXECUTION_WORKER",
            state_machine=LOCAL_EXECUTION_STATE,
        )
        services = ActionServices(
            repository=repository,
            root=self.root,
            docker=docker,
            data=data,
            platform=platform,
            execution=self.execution,
            remote_factory=remote_factory,
            table_inspector=table_inspector,
        )
        self.input_materializer = InputMaterializer(services)
        self.output_collector = OutputCollector(services)
        self.lifecycle = ExecutionLifecycle(services)
        self.artifact_publisher = ArtifactPublisher(services, self.lifecycle)
        self.remote_driver = RemoteExecutionDriver(
            services,
            input_materializer=self.input_materializer,
            output_collector=self.output_collector,
            artifact_publisher=self.artifact_publisher,
            lifecycle=self.lifecycle,
        )
        self.local_driver = LocalExecutionDriver(
            services,
            input_materializer=self.input_materializer,
            output_collector=self.output_collector,
            artifact_publisher=self.artifact_publisher,
            lifecycle=self.lifecycle,
        )

    def step(self, row, root):
        # The process lock serializes Docker commands; the database generation fences writes.
        latest = self.repo.get(action=row["action_id"])
        if latest["cancel_requested"]:
            row["cancel_requested"] = True
        if row["payload"].get("kind") == "WORKFLOW":
            ensure(
                self.workflow is not None, "DEPENDENCY_UNAVAILABLE", "Workflow runtime unavailable"
            )
            return self.workflow.step(row, root)
        if not self.repo.change(row):
            return
        if row.get("parent_execution_id"):
            if row["runtime"]["phase"] == "REPORTING":
                self.artifact_publisher.report_node(row, stopped=True)
                return self.execution.transition(
                    row,
                    "FINAL",
                    state=row["runtime"]["pending_state"],
                    publication=row["runtime"].get("publication"),
                    error=row["runtime"].get("error"),
                    remove_runtime_keys=("pending_state", "node_revision"),
                )
            if (
                row["executed"]
                and row["runtime"].get("node_reported_revision", 0) < row["revision"]
            ):
                self.artifact_publisher.report_node(row)
                return self.execution.change(row, node_reported_revision=row["revision"])
        if self.remote_driver.remote_execution(row):
            return self.remote_driver.run(row, root)
        return self.local_driver.run(row, root)

    def tick(self):
        row = (
            self.repo.claim(self.owner, self.lease_seconds)
            if self.work_classes is None
            else self.repo.claim(self.owner, self.lease_seconds, self.work_classes)
        )
        if row is None:
            return False
        root = self.root / row["execution_id"]
        input_root, output_root = root / "input", root / "output"
        root.mkdir(parents=True, exist_ok=True)
        # WorkflowExecutionDriver exposes the bind root immediately before
        # container creation and seals it after the engine stops.  Preserve
        # that mode while a rootless engine is live; resetting it to 0700 on
        # every poll races Toil's JobStore writes.
        root.chmod(execution_root_mode(row))
        input_root.mkdir(parents=True, exist_ok=True, mode=0o755)
        output_root.mkdir(parents=True, exist_ok=True, mode=0o777)
        # A rootless daemon remaps the explicitly non-root container UID.  The
        # staged input files themselves remain immutable, while these narrow
        # directory modes let that remapped UID traverse inputs and create
        # output paths and list them for the final fsync.  Both permissive
        # mount points remain behind private worker-owned parent directories;
        # the output is tightened as soon as the container stops.  Reapply
        # these modes on recovery because mkdir does not change an existing
        # directory.
        input_root.chmod(0o755)
        output_root.chmod(0o777)
        with ExecutionLeaseGuard(
            self.repo,
            row,
            root,
            self.lease_seconds,
            self.heartbeat_seconds,
        ) as lease:
            if not lease.acquired:
                return True
            self.execution.bind_lease(lease)
            if self.workflow is not None:
                self.workflow.bind_lease(lease)
            try:
                if row.get("parent_execution_id") and row["runtime"]["phase"] == "ACCEPTED":
                    self.execution.trace(
                        row,
                        "EXECUTION_CLAIMED",
                        "first",
                        attributes={"phase": row["runtime"]["phase"]},
                    )
                self.step(row, root)
            except LeaseLostError:
                # A later worker generation owns recovery.  Do not issue a
                # terminal fact or another external effect after lease loss.
                return True
            except (ServiceFault, ValueError, KeyError) as exc:
                if isinstance(exc, ServiceFault) and exc.status >= 500:
                    failures = row["runtime"].get("failures", 0) + 1
                    lease.delay = min(30, 2 ** min(failures - 1, 5))
                    self.repo.change(
                        row, runtime=row["runtime"] | {"uncertain": True, "failures": failures}
                    )
                    logging.warning(
                        "Execution recovery action=%s generation=%s "
                        "category=SERVICE_UNAVAILABLE code=%s detail=%s",
                        row["action_id"],
                        row["generation"],
                        exc.code,
                        str(exc),
                    )
                    return True
                if row["runtime"]["phase"] in (
                    "CREATE_INTENT",
                    "CREATED",
                    "START_INTENT",
                    "RUNNING",
                    "STOPPING_UNSTARTED",
                ):
                    # Runtime identity failure cannot prove that computation has stopped.
                    self.repo.change(row, runtime=row["runtime"] | {"uncertain": True})
                    return True
                # Invalid input is a failed execution only after Data has sealed publication.
                try:
                    error = (
                        row["runtime"].get("remote", {}).get("failure_code")
                        if self.remote_driver.remote_execution(row)
                        else None
                    ) or (exc.code if isinstance(exc, ServiceFault) else type(exc).__name__)
                    terminal = "FAILED" if row["executed"] else "REJECTED"
                    if row.get("payload", {}).get("kind") == "WORKFLOW" and self.workflow:
                        latest = self.repo.get(action=row["action_id"])
                        cancelled = latest["cancel_requested"]
                        self.workflow.lifecycle.abort(
                            row,
                            "CANCELLED" if cancelled else terminal,
                            "CANCELLED" if cancelled else error,
                        )
                    else:
                        self.lifecycle.abort(row, terminal, error)
                except (OSError, ServiceFault):
                    self.repo.change(row, runtime=row["runtime"] | {"uncertain": True})
            except (OSError, subprocess.TimeoutExpired) as exc:
                failures = row["runtime"].get("failures", 0) + 1
                lease.delay = min(30, 2 ** min(failures - 1, 5))
                self.repo.change(
                    row, runtime=row["runtime"] | {"uncertain": True, "failures": failures}
                )
                logging.warning(
                    "Execution recovery action=%s generation=%s category=COMMUNICATION "
                    "phase=%s type=%s",
                    row["action_id"],
                    row["generation"],
                    row["runtime"].get("phase"),
                    type(exc).__name__,
                )
            finally:
                self.execution.bind_lease(None)
                if self.workflow is not None:
                    self.workflow.bind_lease(None)
        return True
