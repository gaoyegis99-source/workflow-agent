"""Consume ordered Knowledge lifecycle facts without rewriting historical reports."""

from platform_core.application.control import digest
from platform_core.domain.control import require


class KnowledgeLifecycleProcessor:
    def __init__(self, store, knowledge, owner):
        self.store, self.knowledge, self.owner = store, knowledge, owner

    def process_one(self):
        claimed = self.knowledge.json(
            "POST",
            "/internal/lifecycle-events/claim",
            json={"owner": self.owner, "lease_seconds": 30},
        ).get("event")
        if claimed is None:
            return False
        event = claimed["payload"]
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
        with self.store.transaction() as uow:
            if uow.delivery.inbox("KNOWLEDGE", event["event_id"], digest(event)):
                uow.science.apply_knowledge_event(event)
        self.knowledge.json(
            "POST",
            f"/internal/lifecycle-events/{event['event_id']}/ack",
            json={"owner": self.owner, "generation": claimed["generation"]},
        )
        return True
