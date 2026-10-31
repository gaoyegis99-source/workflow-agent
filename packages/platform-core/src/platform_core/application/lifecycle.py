"""Consume ordered Knowledge lifecycle facts without rewriting historical reports."""

import threading

from platform_core.application.control import digest
from platform_core.domain.control import require


class KnowledgeLifecycleProcessor:
    def __init__(self, store, knowledge, owner, *, lease_seconds=30, heartbeat_seconds=10):
        if lease_seconds <= 0 or heartbeat_seconds <= 0 or heartbeat_seconds >= lease_seconds:
            raise ValueError("Lifecycle heartbeat must be positive and shorter than the lease")
        self.store, self.knowledge, self.owner = store, knowledge, owner
        self.lease_seconds, self.heartbeat_seconds = lease_seconds, heartbeat_seconds

    def process_one(self):
        claimed = self.knowledge.json(
            "POST",
            "/internal/lifecycle-events/claim",
            json={"owner": self.owner, "lease_seconds": self.lease_seconds},
        ).get("event")
        if claimed is None:
            return False
        lost, stop = threading.Event(), threading.Event()

        def renew() -> bool:
            try:
                self.knowledge.json(
                    "POST",
                    f"/internal/lifecycle-events/{claimed['event_id']}/renew",
                    json={
                        "owner": self.owner,
                        "generation": claimed["generation"],
                        "lease_seconds": self.lease_seconds,
                    },
                )
            except Exception:
                lost.set()
                return False
            return True

        def heartbeat():
            while not stop.wait(self.heartbeat_seconds):
                if not renew():
                    return

        # Renew synchronously before any Platform write, then keep the work
        # lease alive independently from the worker process heartbeat.
        if not renew():
            return True
        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        event = claimed["payload"]
        try:
            require(
                event.get("event_id") == claimed["event_id"]
                and type(event.get("revision")) is int
                and event["revision"] >= 1
                # P5.1 emitted IMPORTED before the P5.3 delivery worker existed.
                # Consume that historical spelling so it cannot block later
                # WITHDRAWN events in the ordered outbox.
                and event.get("kind") in {"CREATED", "IMPORTED", "WITHDRAWN"},
                "INVALID_BINDING",
                "Knowledge lifecycle event is malformed",
            )
            if lost.is_set():
                return True
            with self.store.transaction() as uow:
                if lost.is_set():
                    return True
                if uow.delivery.inbox("KNOWLEDGE", event["event_id"], digest(event)):
                    uow.science.apply_knowledge_event(event)
            if lost.is_set() or not renew():
                return True
            self.knowledge.json(
                "POST",
                f"/internal/lifecycle-events/{event['event_id']}/ack",
                json={"owner": self.owner, "generation": claimed["generation"]},
            )
            return True
        finally:
            stop.set()
            thread.join(timeout=self.heartbeat_seconds + 1)
