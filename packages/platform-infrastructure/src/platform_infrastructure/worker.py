"""Independent outbox worker. P2 production loads no computation adapter."""

import argparse
import logging
import os
import signal
import threading
from uuid import uuid4

from platform_contracts.health import touch_worker_heartbeat
from platform_core.application.acceptance import AcceptanceProcessor
from platform_core.application.dispatch import Dispatcher
from platform_core.application.lifecycle import KnowledgeLifecycleProcessor
from platform_core.application.ports import ExecutionPort
from platform_core.application.workflows import PackageProcessor
from platform_core.domain.control import RuleViolation
from sqlalchemy.exc import SQLAlchemyError

from platform_infrastructure.database import Database
from platform_infrastructure.services import from_environment, knowledge_from_environment

logger = logging.getLogger(__name__)

IDLE_POLL_INITIAL_SECONDS = 0.2
IDLE_POLL_MAX_SECONDS = 5.0


class Worker:
    def __init__(
        self,
        database: Database,
        execution: ExecutionPort | None = None,
        *,
        lease_seconds: float = 30,
        heartbeat_seconds: float = 10,
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
        self.owner = uuid4().hex
        self.lease_seconds, self.heartbeat_seconds = lease_seconds, heartbeat_seconds

    def process_one(self) -> bool:
        claim = self.dispatcher.claim(self.owner, self.lease_seconds)
        if claim is None:
            return False
        stop = threading.Event()

        def heartbeat():
            while not stop.wait(self.heartbeat_seconds):
                try:
                    if not self.dispatcher.renew(claim, self.lease_seconds):
                        return
                except SQLAlchemyError:
                    logger.warning("Lease renewal unavailable event_id=%s", claim["event_id"])
                    return

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            prepared = self.dispatcher.prepare(
                claim, execution_available=self.execution is not None
            )
            if prepared is None:
                return True
            method, action_id = prepared["method"], prepared["action_id"]
            if method == "query":
                observation = self.execution.query(action_id)
                if observation is None:
                    prepared = self.dispatcher.prepare(claim, absent=True)
                    if prepared is None:
                        return True
                    method = prepared["method"]
            if method == "submit":
                observation = self.execution.submit(action_id, prepared["request"])
            elif method == "cancel":
                observation = self.execution.cancel(action_id)
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
            if claim["kind"] == "ACTION":
                self.dispatcher.uncertain(claim)
        finally:
            stop.set()
            thread.join(timeout=self.heartbeat_seconds + 4)
        return True

    def run(self, stop: threading.Event):
        idle_delay = IDLE_POLL_INITIAL_SECONDS
        while not stop.is_set():
            try:
                active = self.packages.process_one() if self.packages else False
                active = self.process_one() or active
                if self.completion:
                    try:
                        active = self.completion.process() or active
                    except RuleViolation:
                        logger.warning("Acceptance dependency unavailable; retrying")
                if self.lifecycle:
                    try:
                        active = self.lifecycle.process_one() or active
                    except RuleViolation:
                        logger.warning("Knowledge lifecycle dependency unavailable; retrying")
            except SQLAlchemyError:
                logger.warning("Worker database unavailable; retrying")
                idle_delay = max(idle_delay, 1.0)
            else:
                if active:
                    idle_delay = IDLE_POLL_INITIAL_SECONDS
                    touch_worker_heartbeat()
                    continue
            # This heartbeat is a process-loop liveness signal, not a work
            # counter. Refresh it after every completed poll so a healthy idle
            # worker does not become unhealthy after the startup touch ages out.
            touch_worker_heartbeat()
            stop.wait(idle_delay)
            idle_delay = min(idle_delay * 2, IDLE_POLL_MAX_SECONDS)


def main():
    parser = argparse.ArgumentParser(description="V7 P2 outbox worker (no computation adapter)")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    # Successful remote claims are expected during normal operation. Keeping
    # httpx at INFO would emit one line per idle poll and obscure useful events.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    database = None
    try:
        database = Database.from_environment()
        database.check_schema()
        data, execution = from_environment()
        knowledge = knowledge_from_environment()
        packages = None
        image_id = os.environ.get("V7_CWL_IMAGE")
        if image_id:
            from platform_infrastructure.cwl import CwlValidator

            packages = PackageProcessor(database, CwlValidator(image_id), owner=uuid4().hex)
        owner = uuid4().hex
        worker = Worker(
            database,
            execution,
            completion=AcceptanceProcessor(database, execution) if execution else None,
            packages=packages,
            lifecycle=(
                KnowledgeLifecycleProcessor(database, knowledge, owner) if knowledge else None
            ),
        )
        if args.once:
            active = packages.process_one() if packages else False
            if not active:
                active = worker.process_one()
            if not active and worker.lifecycle:
                worker.lifecycle.process_one()
        else:
            stop = threading.Event()
            for signum in (signal.SIGTERM, signal.SIGINT):
                signal.signal(signum, lambda *_: stop.set())
            touch_worker_heartbeat()
            worker.run(stop)
    except (SQLAlchemyError, RuntimeError, RuleViolation):
        logger.error("Worker startup failed; check database configuration and migration version")
        raise SystemExit(1) from None
    finally:
        if database:
            database.close()
