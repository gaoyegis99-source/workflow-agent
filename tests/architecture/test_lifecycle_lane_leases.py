"""Unit contracts for the Knowledge lane's leased internal protocol."""

from platform_core.application.lifecycle import KnowledgeLifecycleProcessor


class _Uow:
    class Delivery:
        @staticmethod
        def inbox(source, event_id, digest):
            assert source == "KNOWLEDGE"
            assert event_id == "event"
            assert isinstance(digest, str)
            return True

    class Science:
        def __init__(self):
            self.applied = []

        def apply_knowledge_event(self, event):
            self.applied.append(event)

    def __init__(self):
        self.delivery = self.Delivery()
        self.science = self.Science()

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        return False


class _Store:
    def __init__(self):
        self.uow = _Uow()

    def transaction(self):
        return self.uow


class _Knowledge:
    def __init__(self):
        self.calls = []
        self.claimed = False

    def json(self, method, path, json):
        self.calls.append((method, path, json))
        if path.endswith("/claim"):
            if self.claimed:
                return {"event": None}
            self.claimed = True
            return {
                "event": {
                    "event_id": "event",
                    "generation": 7,
                    "payload": {
                        "event_id": "event",
                        "revision": 1,
                        "kind": "CREATED",
                    },
                }
            }
        if path.endswith("/renew"):
            assert json == {"owner": "lane", "generation": 7, "lease_seconds": 30}
            return {"status": "RENEWED"}
        assert path.endswith("/ack")
        assert json == {"owner": "lane", "generation": 7}
        return {"status": "ACKNOWLEDGED"}


def test_knowledge_lane_renews_before_platform_write_and_acknowledges_fenced_claim():
    store, knowledge = _Store(), _Knowledge()

    assert KnowledgeLifecycleProcessor(store, knowledge, "lane").process_one() is True
    assert [path for _, path, _ in knowledge.calls] == [
        "/internal/lifecycle-events/claim",
        "/internal/lifecycle-events/event/renew",
        "/internal/lifecycle-events/event/renew",
        "/internal/lifecycle-events/event/ack",
    ]
    assert store.uow.science.applied == [{"event_id": "event", "revision": 1, "kind": "CREATED"}]


def test_knowledge_lane_does_not_write_platform_after_renewal_loss():
    class LostKnowledge(_Knowledge):
        def json(self, method, path, json):
            if path.endswith("/renew"):
                self.calls.append((method, path, json))
                raise OSError("lost lease")
            return super().json(method, path, json)

    store, knowledge = _Store(), LostKnowledge()

    assert KnowledgeLifecycleProcessor(store, knowledge, "lane").process_one() is True
    assert store.uow.science.applied == []
    assert not any(path.endswith("/ack") for _, path, _ in knowledge.calls)
