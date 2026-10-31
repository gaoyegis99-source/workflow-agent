"""Execution-owned facts and fenced work leases."""

import hashlib
from datetime import timedelta

import sqlalchemy as sa
from platform_contracts.service import digest, ensure
from sqlalchemy.dialects.postgresql import insert

from platform_execution.adapters.tables import executions as t
from platform_execution.adapters.tables import remote_calls as remote_t
from platform_execution.adapters.tables import remote_observations as remote_observation_t
from platform_execution.adapters.tables import workflow_trace_events as trace_t
from platform_execution.domain.state import TERMINAL, validate_change

WORK_CLASSES = frozenset({"WORKFLOW_SUPERVISOR", "NODE_EXECUTION", "PUBLICATION_RECONCILE"})


class Repository:
    def __init__(self, db):
        self.db = db

    @staticmethod
    def identity(action):
        return "e" + hashlib.sha256(action.encode()).hexdigest()[:32]

    def create(
        self,
        action,
        payload=None,
        *,
        parent_execution_id=None,
        invocation_key=None,
        initial_runtime=None,
        remote_binding=None,
        remote_request_digest=None,
    ):
        execution = self.identity(action)
        work_class = (
            "WORKFLOW_SUPERVISOR"
            if payload and payload.get("kind") == "WORKFLOW" and parent_execution_id is None
            else "NODE_EXECUTION"
        )
        values = dict(
            action_id=action,
            execution_id=execution,
            parent_execution_id=parent_execution_id,
            invocation_key=invocation_key,
            project_id=payload["project_id"] if payload else "unassigned",
            task_id=payload["task_id"] if payload else "unassigned",
            digest=digest(payload) if payload else "",
            payload=payload or {},
            state="QUEUED" if payload else "SEALED",
            revision=1,
            executed=False,
            cancel_requested=payload is None,
            attempt_id="a" + execution[1:],
            runtime={"phase": "ACCEPTED", "publication": "UNKNOWN"} | (initial_runtime or {}),
            work_class=work_class,
        )
        with self.db.engine.begin() as c:
            inserted = c.scalar(
                insert(t).values(**values).on_conflict_do_nothing().returning(t.c.action_id)
            )
            row = dict(
                c.execute(sa.select(t).where(t.c.action_id == action).with_for_update())
                .mappings()
                .one()
            )
            ensure(
                not payload or row["state"] == "SEALED" or row["digest"] == digest(payload),
                "IDEMPOTENCY_CONFLICT",
                "Action request changed",
            )
            if remote_binding is not None:
                if inserted is not None:
                    c.execute(
                        insert(remote_t).values(
                            action_id=action,
                            binding=remote_binding,
                            request_digest=remote_request_digest,
                            phase="STAGING_INPUT",
                        )
                    )
                remote_row = (
                    c.execute(
                        sa.select(remote_t).where(remote_t.c.action_id == action).with_for_update()
                    )
                    .mappings()
                    .first()
                )
                ensure(
                    remote_row is not None,
                    "INVALID_BINDING",
                    "Remote execution has no call ledger",
                )
                remote = dict(remote_row)
                ensure(
                    remote["action_id"] == action
                    and remote["binding"] == remote_binding
                    and remote["request_digest"] == remote_request_digest,
                    "INVALID_BINDING",
                    "Remote call ledger differs from its execution",
                )
        return row

    def create_remote(self, action, binding, request_digest):
        with self.db.engine.begin() as c:
            c.execute(
                insert(remote_t)
                .values(
                    action_id=action,
                    binding=binding,
                    request_digest=request_digest,
                    phase="STAGING_INPUT",
                )
                .on_conflict_do_nothing()
            )
        return self.remote(action)

    def remote(self, action):
        with self.db.engine.connect() as c:
            row = (
                c.execute(sa.select(remote_t).where(remote_t.c.action_id == action))
                .mappings()
                .first()
            )
        ensure(row is not None, "INVALID_BINDING", "Remote call ledger is missing")
        return dict(row)

    def change_remote(self, claim, **changes):
        changes["updated_at"] = sa.func.clock_timestamp()
        with self.db.engine.begin() as c:
            result = c.execute(
                remote_t.update()
                .where(
                    remote_t.c.action_id == claim["action_id"],
                    sa.exists(
                        sa.select(1).where(
                            t.c.action_id == claim["action_id"],
                            t.c.owner == claim["owner"],
                            t.c.generation == claim["generation"],
                            t.c.lease_until > sa.func.clock_timestamp(),
                        )
                    ),
                )
                .values(**changes)
            )
        return result.rowcount == 1

    def observe_remote(self, claim, event, **changes):
        observation_digest = "sha256:" + digest(event)
        changes["updated_at"] = sa.func.clock_timestamp()
        with self.db.engine.begin() as c:
            valid = c.scalar(
                sa.select(sa.literal(True)).where(
                    sa.exists(
                        sa.select(1).where(
                            t.c.action_id == claim["action_id"],
                            t.c.owner == claim["owner"],
                            t.c.generation == claim["generation"],
                            t.c.lease_until > sa.func.clock_timestamp(),
                        )
                    )
                )
            )
            if not valid:
                return False
            existing = (
                c.execute(
                    sa.select(remote_observation_t).where(
                        remote_observation_t.c.action_id == claim["action_id"],
                        remote_observation_t.c.event_id == event["event_id"],
                    )
                )
                .mappings()
                .first()
            )
            ensure(
                existing is None or existing["observation_digest"] == observation_digest,
                "INVALID_BINDING",
                "Remote event identity changed",
            )
            if existing is None:
                c.execute(
                    remote_observation_t.insert().values(
                        action_id=claim["action_id"],
                        event_id=event["event_id"],
                        revision=event["revision"],
                        observation_digest=observation_digest,
                        observation=event,
                    )
                )
            c.execute(
                remote_t.update()
                .where(remote_t.c.action_id == claim["action_id"])
                .values(**changes)
            )
        return True

    def children(self, parent):
        with self.db.engine.connect() as c:
            return [
                dict(row)
                for row in c.execute(
                    sa.select(t)
                    .where(t.c.parent_execution_id == parent)
                    .order_by(t.c.invocation_key)
                ).mappings()
            ]

    def cancel_children(self, parent):
        with self.db.engine.begin() as c:
            c.execute(
                t.update()
                .where(t.c.parent_execution_id == parent, t.c.state.not_in(TERMINAL))
                .values(cancel_requested=True, available_at=sa.func.clock_timestamp())
            )
        return self.children(parent)

    def append_trace(self, event):
        """Append one diagnostic fact in its own transaction; never update trace history."""
        with self.db.engine.begin() as c:
            inserted = c.scalar(
                insert(trace_t)
                .values(**event)
                .on_conflict_do_nothing()
                .returning(trace_t.c.event_key)
            )
        return inserted is not None

    def traces(self, workflow_execution_id):
        """Internal test/report access; no HTTP route exposes diagnostic history."""
        with self.db.engine.connect() as c:
            return [
                dict(row)
                for row in c.execute(
                    sa.select(trace_t)
                    .where(trace_t.c.workflow_execution_id == workflow_execution_id)
                    .order_by(trace_t.c.observed_at, trace_t.c.event_key)
                ).mappings()
            ]

    def get(self, action=None, execution=None, project=None):
        query = sa.select(t)
        query = (
            query.where(t.c.action_id == action)
            if action
            else query.where(t.c.execution_id == execution)
        )
        if project:
            query = query.where(t.c.project_id == project)
        with self.db.engine.connect() as c:
            row = c.execute(query).mappings().first()
            ensure(row is not None, "RESOURCE_NOT_FOUND", "Execution not found", 404)
            return dict(row)

    def cancel(self, action):
        self.create(action)
        with self.db.engine.begin() as c:
            c.execute(
                t.update()
                .where(t.c.action_id == action)
                .values(cancel_requested=True, available_at=sa.func.now())
            )
        return self.get(action=action)

    def claim(self, owner, lease_seconds=30, work_classes=None):
        if work_classes is not None:
            work_classes = tuple(work_classes)
            ensure(
                bool(work_classes) and set(work_classes) <= WORK_CLASSES,
                "INVALID_BINDING",
                "Invalid execution work class",
            )
        with self.db.engine.begin() as c:
            now = c.scalar(sa.select(sa.func.clock_timestamp()))
            ready = (
                t.c.state.not_in(TERMINAL),
                t.c.available_at <= now,
                sa.or_(t.c.lease_until.is_(None), t.c.lease_until < now),
            )
            if work_classes is not None:
                ready += (t.c.work_class.in_(work_classes),)
            row = (
                c.execute(
                    sa.select(t)
                    .where(*ready)
                    .order_by(t.c.available_at)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                .mappings()
                .first()
            )
            if row is None:
                return None
            result = dict(row) | {
                "owner": owner,
                "generation": row["generation"] + 1,
                "lease_until": now + timedelta(seconds=lease_seconds),
            }
            c.execute(
                t.update()
                .where(t.c.action_id == row["action_id"])
                .values(
                    owner=owner, generation=result["generation"], lease_until=result["lease_until"]
                )
            )
            return result

    def renew(self, claim, lease_seconds=30):
        with self.db.engine.begin() as c:
            now = c.scalar(sa.select(sa.func.clock_timestamp()))
            return (
                c.execute(
                    t.update()
                    .where(
                        t.c.action_id == claim["action_id"],
                        t.c.owner == claim["owner"],
                        t.c.generation == claim["generation"],
                        t.c.lease_until > now,
                        t.c.state.not_in(TERMINAL),
                    )
                    .values(lease_until=now + timedelta(seconds=lease_seconds))
                ).rowcount
                == 1
            )

    def claim_is_current(self, claim):
        """Synchronously verify a claim before an external side effect."""

        with self.db.engine.connect() as c:
            return bool(
                c.scalar(
                    sa.select(sa.literal(True)).where(
                        t.c.action_id == claim["action_id"],
                        t.c.owner == claim["owner"],
                        t.c.generation == claim["generation"],
                        t.c.lease_until > sa.func.clock_timestamp(),
                        t.c.state.not_in(TERMINAL),
                    )
                )
            )

    def transition(self, claim, *, source_phase, runtime, **changes):
        """Persist one lifecycle transition under the claim and source-phase fence.

        The current row is locked first, so a stale worker cannot turn an old
        in-memory phase into a new durable phase after another generation has
        recovered the same execution.
        """

        if "phase" in changes or "runtime" in changes:
            raise ValueError("EXECUTION_TRANSITION_INVALID")
        with self.db.engine.begin() as c:
            row = (
                c.execute(sa.select(t).where(t.c.action_id == claim["action_id"]).with_for_update())
                .mappings()
                .one()
            )
            now = c.scalar(sa.select(sa.func.clock_timestamp()))
            if (
                (row["owner"], row["generation"]) != (claim["owner"], claim["generation"])
                or row["lease_until"] <= now
                or row["runtime"].get("phase") != source_phase
            ):
                return False
            changes["runtime"] = runtime
            validate_change(row, changes)
            ensure(
                not row["executed"] or changes.get("executed", True),
                "INVALID_BINDING",
                "Execution fact cannot be retracted",
            )
            if changes.get("state") in TERMINAL:
                changes["runtime"].pop("uncertain", None)
                changes["runtime"].pop("failures", None)
            if changes["runtime"].get("phase") == "PUBLISHING":
                changes["work_class"] = "PUBLICATION_RECONCILE"
            if any(changes.get(key, row[key]) != row[key] for key in ("state", "executed")):
                changes["revision"] = row["revision"] + 1
            c.execute(t.update().where(t.c.action_id == claim["action_id"]).values(**changes))
            claim.update(changes)
            return True

    def change(self, claim, **changes):
        with self.db.engine.begin() as c:
            row = (
                c.execute(sa.select(t).where(t.c.action_id == claim["action_id"]).with_for_update())
                .mappings()
                .one()
            )
            now = c.scalar(sa.select(sa.func.clock_timestamp()))
            if (row["owner"], row["generation"]) != (claim["owner"], claim["generation"]) or row[
                "lease_until"
            ] <= now:
                return False
            runtime = changes.get("runtime")
            if (
                "state" in changes
                or "executed" in changes
                or (runtime is not None and runtime.get("phase") != row["runtime"].get("phase"))
            ):
                raise ValueError("EXECUTION_TRANSITION_REQUIRED")
            if not changes:
                return True
            validate_change(row, changes)
            ensure(
                not row["executed"] or changes.get("executed", True),
                "INVALID_BINDING",
                "Execution fact cannot be retracted",
            )
            if changes.get("state") in TERMINAL and "runtime" in changes:
                changes["runtime"].pop("uncertain", None)
                changes["runtime"].pop("failures", None)
            if changes.get("runtime", row["runtime"]).get("phase") == "PUBLISHING":
                changes["work_class"] = "PUBLICATION_RECONCILE"
            if any(changes.get(k, row[k]) != row[k] for k in ("state", "executed")):
                changes["revision"] = row["revision"] + 1
            c.execute(t.update().where(t.c.action_id == claim["action_id"]).values(**changes))
            claim.update(changes)
            return True

    def release(self, claim, delay=0.2):
        return self.change(
            claim,
            owner=None,
            lease_until=None,
            available_at=sa.func.clock_timestamp() + timedelta(seconds=delay),
        )

    @staticmethod
    def observation(row):
        ensure(
            not row["runtime"].get("uncertain"),
            "DEPENDENCY_UNAVAILABLE",
            "Execution or publication facts are temporarily unknown",
            503,
        )
        return {
            "event_id": f"{row['execution_id']}:{row['revision']}",
            "action_id": row["action_id"],
            "revision": row["revision"],
            "state": row["state"],
            "execution_id": None if row["state"] == "SEALED" else row["execution_id"],
            "executed": row["executed"],
        }

    @staticmethod
    def view(row):
        from platform_contracts.engine_runtime import diagnostic

        runtime = dict(row["runtime"])
        frozen_profile = runtime.pop("_engine_runtime_profile", None)
        if row["payload"].get("kind") == "WORKFLOW":
            ensure(
                frozen_profile is not None,
                "INVALID_BINDING",
                "Workflow engine profile is missing",
                422,
            )
            runtime["engine_profile"] = diagnostic(frozen_profile)
        request = dict(row["payload"])
        request.pop("_remote_binding", None)
        request.pop("_remote_bindings", None)
        return {
            key: row[key]
            for key in (
                "action_id",
                "execution_id",
                "project_id",
                "task_id",
                "state",
                "revision",
                "executed",
                "attempt_id",
                "container_id",
                "cancel_requested",
            )
        } | {"request": request, "runtime": runtime}
