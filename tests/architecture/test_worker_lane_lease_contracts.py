"""No-database contracts for the per-lane claim/renew/fence shape."""

from platform_core.application.acceptance import AcceptanceProcessor
from platform_core.application.catalog import build_catalog
from platform_core.application.workflows import PackageProcessor


class _Context:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self.value

    def __exit__(self, *unused):
        return False


def test_package_processor_fences_before_validation_when_claim_is_lost(monkeypatch):
    claim = {"package_id": "package", "generation": 1, "request": {}}

    class Workflows:
        claimed = False
        finished = []

        def claim_package(self, owner, seconds):
            if self.claimed:
                return None
            self.claimed = True
            return claim | {"owner": owner}

        @staticmethod
        def package_owned(*unused):
            return False

        @staticmethod
        def renew_package(*unused):
            raise AssertionError("heartbeat must not run before the initial fence")

        @staticmethod
        def release_package(*unused):
            raise AssertionError("lost claims are left for recovery")

        def finish_package(self, *args, **kwargs):
            self.finished.append((args, kwargs))

    class Authoring:
        @staticmethod
        def candidate_by_package(package_id):
            assert package_id == "package"
            return None

    class Store:
        def __init__(self):
            self.uow = type("Uow", (), {"workflows": Workflows(), "authoring": Authoring()})()

        def transaction(self):
            return _Context(self.uow)

    class Validator:
        image_id = "validator"

        @staticmethod
        def validate(compiled):
            raise AssertionError("lease loss must fence validation")

    processor = PackageProcessor(
        Store(), Validator(), operator_catalog=build_catalog(), owner="lane"
    )
    monkeypatch.setattr(
        processor,
        "freeze",
        lambda request, **_kwargs: {"manifest_digest": "sha256:test"},
    )

    assert processor.process_one() is True
    assert processor.store.uow.workflows.finished == []


def test_acceptance_processor_claims_and_releases_one_ready_item_without_batch_drain():
    class Science:
        def __init__(self):
            self.claims = 0
            self.releases = []

        def claim_acceptance(self, owner, seconds):
            self.claims += 1
            return {"acceptance_id": "acceptance", "owner": owner, "generation": 3}

        @staticmethod
        def active_r2_checks(acceptance_id):
            assert acceptance_id == "acceptance"
            return []

        @staticmethod
        def pending_checks(acceptance_id):
            assert acceptance_id == "acceptance"
            return []

        @staticmethod
        def renew_acceptance(*unused):
            return True

        def release_acceptance(self, acceptance_id, owner, generation):
            self.releases.append((acceptance_id, owner, generation))
            return True

        finish_acceptance = release_acceptance

    class Store:
        def __init__(self):
            self.uow = type("Uow", (), {"science": Science()})()

        def transaction(self):
            return _Context(self.uow)

    processor = AcceptanceProcessor(Store(), object(), owner="lane")

    assert processor.process_one() is True
    assert processor.store.uow.science.claims == 1
    assert processor.store.uow.science.releases == [("acceptance", "lane", 3)]
