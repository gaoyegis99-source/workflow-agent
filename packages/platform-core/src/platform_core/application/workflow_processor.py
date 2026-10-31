"""Leased asynchronous compiler/validator for workflow packages."""

from __future__ import annotations

import hashlib
import json
import threading
from typing import Protocol

from platform_core.application.ports import Record, Store
from platform_core.application.workflow_compiler import compile_package
from platform_core.domain.authoring import canonical_digest
from platform_core.domain.control import RuleViolation, require


class PackageValidator(Protocol):
    image_id: str

    def validate(self, compiled: Record) -> None: ...


class PackageProcessor:
    """Compile outside the transaction, then publish under the original lease generation."""

    def __init__(
        self,
        store: Store,
        validator: PackageValidator,
        *,
        operator_catalog: Record,
        owner: str,
        lease_seconds: float = 120,
        heartbeat_seconds: float | None = None,
    ):
        if lease_seconds <= 0:
            raise ValueError("Package lease must be positive")
        heartbeat_seconds = heartbeat_seconds or min(30.0, lease_seconds / 3)
        if heartbeat_seconds <= 0 or heartbeat_seconds >= lease_seconds:
            raise ValueError("Package heartbeat must be positive and shorter than the lease")
        self.store = store
        self.validator = validator
        self.operator_catalog = operator_catalog
        self.owner = owner
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds

    @staticmethod
    def freeze(request: Record, *, operator_catalog: Record) -> Record:
        compiled = compile_package(request, catalog=operator_catalog)
        frozen = json.loads(
            json.dumps(compiled, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        )
        manifest = hashlib.sha256(
            json.dumps(frozen, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return frozen | {"manifest_digest": "sha256:" + manifest}

    def process_one(self) -> bool:
        with self.store.transaction() as uow:
            claim = uow.workflows.claim_package(self.owner, self.lease_seconds)
            candidate = uow.authoring.candidate_by_package(claim["package_id"]) if claim else None
        if claim is None:
            return False
        stop, lost = threading.Event(), threading.Event()

        def heartbeat():
            while not stop.wait(self.heartbeat_seconds):
                try:
                    with self.store.transaction() as uow:
                        renewed = uow.workflows.renew_package(
                            claim["package_id"],
                            self.owner,
                            claim["generation"],
                            self.lease_seconds,
                        )
                except Exception:  # database transport failure: fail closed below
                    renewed = False
                if not renewed:
                    lost.set()
                    return

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()

        def owned() -> bool:
            if lost.is_set():
                return False
            try:
                with self.store.transaction() as uow:
                    current = uow.workflows.package_owned(
                        claim["package_id"], self.owner, claim["generation"]
                    )
            except Exception:
                lost.set()
                return False
            if not current:
                lost.set()
            return current

        try:
            # The compile is pure, but validation can call a remote/runtime
            # adapter.  Fence it immediately before that external effect.
            if not owned():
                return True
            compiled = self.freeze(claim["request"], operator_catalog=self.operator_catalog)
            if candidate is not None:
                current = {
                    "catalog_digest": self.operator_catalog["catalog_digest"],
                    "compiler_digest": canonical_digest(
                        {"compiler": "bounded-workflow-compiler", "version": "a5-v1"}
                    ),
                    "validator_image_digest": self.validator.image_id,
                }
                frozen = candidate["context"]
                require(
                    all(frozen.get(key) == value for key, value in current.items()),
                    "VALIDATION_ENVIRONMENT_CHANGED",
                    "Candidate validation environment changed",
                    409,
                )
                require(
                    compiled == candidate["expected_manifest"],
                    "INVALID_BINDING",
                    "Candidate compiled manifest differs from the frozen expectation",
                    409,
                )
            if not owned():
                return True
            self.validator.validate(compiled)
            if not owned():
                return True
            status, error = "READY", None
        except RuleViolation as problem:
            compiled = None
            status, error = "REJECTED", {"code": problem.code, "message": problem.message}
        except (OSError, TimeoutError):
            # Infrastructure failure must be retried by a later lease owner, not
            # converted into a permanent rejection of the user's package.
            if not lost.is_set():
                with self.store.transaction() as uow:
                    uow.workflows.release_package(
                        claim["package_id"], self.owner, claim["generation"]
                    )
            return True
        finally:
            # Do not leave a live heartbeat after a worker has stopped owning
            # the row.  A failed/lost lease intentionally expires for recovery.
            stop.set()
            thread.join(timeout=self.heartbeat_seconds + 1)

        if lost.is_set():
            return True
        with self.store.transaction() as uow:
            uow.workflows.finish_package(
                claim["package_id"],
                self.owner,
                claim["generation"],
                status=status,
                compiled=compiled,
                error=error,
            )
        return True
