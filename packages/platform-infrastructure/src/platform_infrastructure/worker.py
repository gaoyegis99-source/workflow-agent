"""Independent outbox worker. P2 production loads no computation adapter."""

import argparse
import logging
import signal
import threading
from enum import StrEnum
from pathlib import Path
from uuid import uuid4

from platform_contracts.health import touch_worker_heartbeat
from platform_core.application.acceptance import AcceptanceProcessor
from platform_core.application.dispatch import Dispatcher
from platform_core.application.lifecycle import KnowledgeLifecycleProcessor
from platform_core.application.ports import ExecutionPort
from platform_core.application.workflows import PackageProcessor
from platform_core.domain.control import RuleViolation
from sqlalchemy.exc import SQLAlchemyError

from platform_infrastructure.composition import PlatformWorkerComposition
from platform_infrastructure.database import Database
from platform_infrastructure.services import execution_client, knowledge_client

logger = logging.getLogger(__name__)

IDLE_POLL_INITIAL_SECONDS = 0.2
IDLE_POLL_MAX_SECONDS = 5.0


class WorkerLane(StrEnum):
    """One independently deployable platform worker responsibility."""

    PACKAGE = "package"
    ACTION = "action"
    ACCEPTANCE = "acceptance"
    KNOWLEDGE = "knowledge"


class Worker:
    def __init__(
        self,
        database: Database,
        execution: ExecutionPort | None = None,
        *,
        lane: WorkerLane | str,
        lease_seconds: float = 30,
        heartbeat_seconds: float = 10,
        heartbeat_file: Path | None = None,
        completion=None,
        packages=None,
        lifecycle=None,
    ):
        if lease_seconds <= 0 or heartbeat_seconds <= 0 or heartbeat_seconds >= lease_seconds:
            raise ValueError("Heartbeat must be positive and shorter than the lease")
        self.dispatcher = Dispatcher(database)
        self.execution = execution
        self.completion = completion
        self.packages = packages
        self.lifecycle = lifecycle
        self.lane = WorkerLane(lane)
        self.owner = uuid4().hex
        self.lease_seconds, self.heartbeat_seconds = lease_seconds, heartbeat_seconds
        self.heartbeat_file = heartbeat_file

    def process_one(self) -> bool:
        """Process at most one item for this worker's configured lane."""

        if self.lane is WorkerLane.PACKAGE:
            return bool(self.packages and self.packages.process_one())
        if self.lane is WorkerLane.ACCEPTANCE:
            return bool(self.completion and self.completion.process_one())
        if self.lane is WorkerLane.KNOWLEDGE:
            return bool(self.lifecycle and self.lifecycle.process_one())
        return self._process_action()

    def _process_action(self) -> bool:
        claim = self.dispatcher.claim(self.owner, self.lease_seconds)
        if claim is None:
            return False
        stop, lost = threading.Event(), threading.Event()

        def heartbeat():
            while not stop.wait(self.heartbeat_seconds):
                try:
                    if not self.dispatcher.renew(claim, self.lease_seconds):
                        lost.set()
                        return
                except SQLAlchemyError:
                    lost.set()
                    logger.warning("Lease renewal unavailable event_id=%s", claim["event_id"])
                    return

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()

        def owned() -> bool:
            """Fail closed before every remote side effect or durable observation."""

            if lost.is_set():
                return False
            try:
                current = self.dispatcher.owned(claim)
            except SQLAlchemyError:
                current = False
            if not current:
                lost.set()
            return current

        try:
            if not owned():
                return True
            prepared = self.dispatcher.prepare(
                claim, execution_available=self.execution is not None
            )
            if prepared is None:
                return True
            method, action_id = prepared["method"], prepared["action_id"]
            if method == "query":
                if not owned():
                    return True
                observation = self.execution.query(action_id)
                if observation is None:
                    if not owned():
                        return True
                    prepared = self.dispatcher.prepare(claim, absent=True)
                    if prepared is None:
                        return True
                    method = prepared["method"]
            if method == "submit":
                if not owned():
                    return True
                observation = self.execution.submit(action_id, prepared["request"])
            elif method == "cancel":
                if not owned():
                    return True
                observation = self.execution.cancel(action_id)
            if not owned():
                return True
            if self.dispatcher.observe(claim, observation):
                logger.info(
                    "Action observation recorded action_id=%s event_id=%r generation=%s state=%s",
                    action_id,
                    observation.event_id,
                    claim["generation"],
                    observation.state,
                )
            else:
                logger.info(
                    "Stale worker observation ignored action_id=%s event_id=%s generation=%s",
                    action_id,
                    claim["event_id"],
                    claim["generation"],
                )
        except (OSError, TimeoutError, ValueError, TypeError, RuleViolation) as error:
            # No payloads, credentials, SQL, or external response bodies in logs.
            logger.warning(
                "Action reconciliation needed action_id=%s event_id=%s "
                "generation=%s type=%s code=%s",
                claim["payload"].get("action_id"),
                claim["event_id"],
                claim["generation"],
                type(error).__name__,
                error.code if isinstance(error, RuleViolation) else "COMMUNICATION_FAILURE",
            )
            if claim["kind"] == "ACTION" and owned():
                self.dispatcher.uncertain(claim)
        finally:
            stop.set()
            thread.join(timeout=self.heartbeat_seconds + 4)
        return True

    def run(self, stop: threading.Event):
        idle_delay = IDLE_POLL_INITIAL_SECONDS
        while not stop.is_set():
            try:
                active = self.process_one()
            except RuleViolation:
                logger.warning(
                    "Worker lane dependency unavailable lane=%s; retrying", self.lane.value
                )
            except SQLAlchemyError:
                logger.warning("Worker database unavailable lane=%s; retrying", self.lane.value)
                idle_delay = max(idle_delay, 1.0)
            else:
                if active:
                    idle_delay = IDLE_POLL_INITIAL_SECONDS
                    touch_worker_heartbeat(self.heartbeat_file)
                    continue
            # This heartbeat is a process-loop liveness signal, not a work
            # counter. Refresh it after every completed poll so a healthy idle
            # worker does not become unhealthy after the startup touch ages out.
            touch_worker_heartbeat(self.heartbeat_file)
            stop.wait(idle_delay)
            idle_delay = min(idle_delay * 2, IDLE_POLL_MAX_SECONDS)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="V7 P2 outbox worker (no computation adapter)")
    parser.add_argument("--lane", required=True, choices=[lane.value for lane in WorkerLane])
    parser.add_argument("--once", action="store_true")
    return parser.parse_args(argv)


def main():
    args = parse_args()
    logging.basicConfig(level=logging.INFO)
    # Successful remote claims are expected during normal operation. Keeping
    # httpx at INFO would emit one line per idle poll and obscure useful events.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    database = None
    try:
        lane = WorkerLane(args.lane)
        composition = PlatformWorkerComposition.from_environment(lane=lane.value)
        database = Database(composition.database_url)
        database.check_schema()
        execution = None
        packages = None
        completion = None
        lifecycle = None
        if lane is WorkerLane.PACKAGE:
            image_id = composition.cwl_image
            if not image_id:
                raise RuntimeError("Package lane requires V7_CWL_IMAGE")
            if composition.operator_catalog is None:
                raise RuntimeError("Package lane requires V7_OPERATOR_CATALOG")
            from platform_infrastructure.cwl import CwlValidator

            packages = PackageProcessor(
                database,
                CwlValidator(image_id),
                operator_catalog=composition.operator_catalog,
                owner=uuid4().hex,
            )
        elif lane in {WorkerLane.ACTION, WorkerLane.ACCEPTANCE}:
            execution = execution_client(composition.execution, deployment=composition.deployment)
            if execution is None:
                raise RuntimeError(f"{lane.value} lane requires Execution configuration")
            if lane is WorkerLane.ACCEPTANCE:
                completion = AcceptanceProcessor(database, execution, owner=uuid4().hex)
        elif lane is WorkerLane.KNOWLEDGE:
            knowledge = knowledge_client(composition.knowledge, deployment=composition.deployment)
            if knowledge is None:
                raise RuntimeError("Knowledge lane requires Knowledge configuration")
            lifecycle = KnowledgeLifecycleProcessor(database, knowledge, uuid4().hex)
        worker = Worker(
            database,
            execution,
            completion=completion,
            packages=packages,
            lifecycle=lifecycle,
            lane=lane,
            heartbeat_file=composition.heartbeat_file,
        )
        if args.once:
            worker.process_one()
        else:
            stop = threading.Event()
            for signum in (signal.SIGTERM, signal.SIGINT):
                signal.signal(signum, lambda *_: stop.set())
            touch_worker_heartbeat(composition.heartbeat_file)
            worker.run(stop)
    except (SQLAlchemyError, RuntimeError, RuleViolation):
        logger.error("Worker startup failed; check database configuration and migration version")
        raise SystemExit(1) from None
    finally:
        if database:
            database.close()
