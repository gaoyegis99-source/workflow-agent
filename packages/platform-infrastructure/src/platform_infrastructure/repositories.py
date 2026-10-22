"""SQLAlchemy Core repositories. Transactions are owned by the application UoW."""

import hashlib
import json
from dataclasses import asdict
from datetime import timedelta

import sqlalchemy as sa
from platform_core.domain.control import Budget, Task, require
from sqlalchemy.dialects.postgresql import insert

from platform_infrastructure import tables as t


def digest_record(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def one(connection, query, *, required=True):
    row = connection.execute(query).mappings().first()
    if required:
        require(row is not None, "RESOURCE_NOT_FOUND", "Resource not found", 404)
    return dict(row) if row else None


class Repository:
    def __init__(self, connection):
        self.c = connection

    def now(self):
        return self.c.scalar(sa.select(sa.func.clock_timestamp()))


class AccessRepository(Repository):
    def authorize(self, actor, project_id, role="VIEWER"):
        token = one(
            self.c,
            sa.select(t.tokens)
            .where(
                t.tokens.c.token_id == actor.token_id,
                t.tokens.c.principal_id == actor.principal_id,
            )
            .with_for_update(read=True),
            required=False,
        )
        require(
            token is not None and not token["revoked"] and token["expires_at"] > self.now(),
            "AUTHENTICATION_REQUIRED",
            "Valid authentication is required",
            401,
        )
        if project_id is None:
            return
        member = one(
            self.c,
            sa.select(t.members)
            .where(
                t.members.c.project_id == project_id,
                t.members.c.principal_id == actor.principal_id,
            )
            .with_for_update(read=True),
            required=False,
        )
        require(member is not None, "RESOURCE_NOT_FOUND", "Resource not found", 404)
        levels = {"VIEWER": 0, "EDITOR": 1, "OWNER": 2}
        require(
            levels[member["role"]] >= levels[role],
            "SCOPE_DENIED",
            "Project role is insufficient",
            403,
        )

    def authorize_principal(self, principal_id, project_id, role="VIEWER"):
        """Authorize a service-side continuation of an authenticated Agent task.

        Agent workers never receive or retain a user's bearer token.  The run
        freezes the initiating principal and every tool read rechecks current
        project membership through this service identity path.
        """
        member = one(
            self.c,
            sa.select(t.members)
            .where(
                t.members.c.project_id == project_id,
                t.members.c.principal_id == principal_id,
            )
            .with_for_update(read=True),
            required=False,
        )
        require(member is not None, "RESOURCE_NOT_FOUND", "Resource not found", 404)
        levels = {"VIEWER": 0, "EDITOR": 1, "OWNER": 2}
        require(
            levels[member["role"]] >= levels[role],
            "SCOPE_DENIED",
            "Project role is insufficient",
            403,
        )

    def role(self, actor, project_id):
        return one(
            self.c,
            sa.select(t.members).where(
                t.members.c.project_id == project_id,
                t.members.c.principal_id == actor.principal_id,
            ),
        )["role"]

    def projects(self, actor, after, limit):
        rows = self.c.execute(
            sa.select(t.projects, t.members.c.role)
            .join(t.members)
            .where(
                t.members.c.principal_id == actor.principal_id,
                t.projects.c.project_id > after,
            )
            .order_by(t.projects.c.project_id)
            .limit(limit)
        ).mappings()
        return [dict(row) for row in rows]

    def project(self, project_id):
        return one(self.c, sa.select(t.projects).where(t.projects.c.project_id == project_id))

    def create_project(self, project_id, name, actor):
        self.c.execute(t.projects.insert().values(project_id=project_id, name=name))
        self.c.execute(
            t.members.insert().values(
                project_id=project_id, principal_id=actor.principal_id, role="OWNER"
            )
        )
        return self.project(project_id) | {"role": "OWNER"}


class TaskRepository(Repository):
    def get(self, project_id, task_id, *, lock=False):
        query = sa.select(t.tasks).where(
            t.tasks.c.project_id == project_id, t.tasks.c.task_id == task_id
        )
        return Task(**one(self.c, query.with_for_update() if lock else query))

    def save(self, task, *, new=False):
        values = asdict(task)
        statement = (
            t.tasks.insert() if new else t.tasks.update().where(t.tasks.c.task_id == task.task_id)
        )
        self.c.execute(statement.values(**values))

    def add_spec(self, task_id, revision, spec, actor_id):
        self.c.execute(
            t.specs.insert().values(
                task_id=task_id, revision=revision, spec=spec, actor_id=actor_id
            )
        )

    def spec(self, task_id, revision):
        return one(
            self.c,
            sa.select(t.specs).where(t.specs.c.task_id == task_id, t.specs.c.revision == revision),
        )["spec"]

    def list(self, project_id, after, limit):
        rows = self.c.execute(
            sa.select(t.tasks)
            .where(
                t.tasks.c.project_id == project_id,
                t.tasks.c.task_id > after,
            )
            .order_by(t.tasks.c.task_id)
            .limit(limit)
        ).mappings()
        return [Task(**row) for row in rows]

    def state_counts(self, project_id):
        status = sa.case(
            (
                sa.and_(t.tasks.c.lifecycle == "OPEN", t.tasks.c.control == "PAUSED"),
                "PAUSED",
            ),
            else_=t.tasks.c.lifecycle,
        ).label("state")
        return {
            row["state"]: row["count"]
            for row in self.c.execute(
                sa.select(status, sa.func.count().label("count"))
                .where(t.tasks.c.project_id == project_id)
                .group_by(status)
            ).mappings()
        }

    def grants(self, project_id, after, limit):
        return [
            dict(row)
            for row in self.c.execute(
                sa.select(t.grants)
                .where(
                    t.grants.c.project_id == project_id,
                    t.grants.c.grant_id > after,
                )
                .order_by(t.grants.c.grant_id)
                .limit(limit)
            ).mappings()
        ]

    def active_grants(self, project_id, task_id, now):
        return [
            dict(row)
            for row in self.c.execute(
                sa.select(t.grants)
                .where(
                    t.grants.c.project_id == project_id,
                    t.grants.c.task_id == task_id,
                    t.grants.c.revoked.is_(False),
                    t.grants.c.expires_at > now,
                )
                .order_by(t.grants.c.grant_id)
            ).mappings()
        ]

    def grant(self, project_id, grant_id):
        return one(
            self.c,
            sa.select(t.grants).where(
                t.grants.c.project_id == project_id, t.grants.c.grant_id == grant_id
            ),
        )

    def add_grant(self, record):
        self.c.execute(t.grants.insert().values(**record))

    def revoke_grant(self, grant_id, actor_id):
        self.c.execute(
            t.grants.update()
            .where(t.grants.c.grant_id == grant_id)
            .values(revoked=True, revoked_at=self.now(), revoked_by=actor_id)
        )

    def permits(self, task_id, target, resources, now, *, derived=False):
        checking = target.get("operator_id") in {"p3-timeseries-check", "open-science-check"}
        acceptance_profile = {
            "p3-timeseries-check": "p3-timeseries-v1",
            "open-science-check": "open-science-v1",
        }.get(target.get("operator_id"))
        columns = [t.grants.c.targets, t.grants.c.resources, t.grants.c.derived_outputs]
        if checking:
            columns += [t.grants.c.acceptance_profiles]
        rows = self.c.execute(
            sa.select(*columns).where(
                t.grants.c.task_id == task_id,
                t.grants.c.revoked.is_(False),
                t.grants.c.expires_at > now,
            )
        ).mappings()
        # Validation is a distinct permission and requires a verified task-derived candidate.
        # The application freezes provenance; resources remain the exact original inputs.
        return any(
            all(r in row["resources"] for r in resources)
            and (not derived or row["derived_outputs"])
            and (
                (row["derived_outputs"] and acceptance_profile in row["acceptance_profiles"])
                if checking
                else target in row["targets"]
            )
            for row in rows
        )


class BudgetRepository(Repository):
    def get(self, task_id):
        return Budget(**one(self.c, sa.select(t.budgets).where(t.budgets.c.task_id == task_id)))

    def save(self, budget, *, new=False):
        statement = (
            t.budgets.insert()
            if new
            else t.budgets.update().where(t.budgets.c.task_id == budget.task_id)
        )
        self.c.execute(statement.values(**asdict(budget)))

    def reserve(self, action_id, task_id):
        self.c.execute(
            t.reservations.insert().values(
                action_id=action_id, task_id=task_id, charged=False, stopped=False
            )
        )

    def reservation(self, action_id):
        return one(self.c, sa.select(t.reservations).where(t.reservations.c.action_id == action_id))

    def update_reservation(self, action_id, *, source, event_id, charged, stopped):
        previous = self.reservation(action_id)
        if previous["charged"] != charged or previous["stopped"] != stopped:
            self.c.execute(
                t.settlements.insert().values(
                    action_id=action_id,
                    source=source,
                    event_id=event_id,
                    charged=charged,
                    stopped=stopped,
                )
            )
        self.c.execute(
            t.reservations.update()
            .where(t.reservations.c.action_id == action_id)
            .values(charged=charged, stopped=stopped)
        )


class ActionRepository(Repository):
    def by_execution(self, project, execution):
        return one(
            self.c,
            sa.select(t.actions).where(
                t.actions.c.project_id == project, t.actions.c.execution_id == execution
            ),
        )

    def list(self, task_id):
        return [
            dict(row)
            for row in self.c.execute(
                sa.select(
                    t.actions.c.action_id,
                    t.actions.c.command_id,
                    t.actions.c.execution_id,
                    t.actions.c.state,
                    t.actions.c.spec_revision,
                    t.actions.c.stopped,
                    t.actions.c.cancel_requested,
                    t.actions.c.request,
                )
                .where(t.actions.c.task_id == task_id)
                .order_by(t.actions.c.action_id)
            ).mappings()
        ]

    def project_page(self, project_id, after, limit, *, task_id=None, state=None):
        query = sa.select(
            t.actions.c.action_id,
            t.actions.c.execution_id,
            t.actions.c.task_id,
            t.actions.c.state,
            t.actions.c.spec_revision,
            t.actions.c.decision_source,
            t.actions.c.request,
            t.actions.c.stopped,
            t.actions.c.cancel_requested,
        ).where(
            t.actions.c.project_id == project_id,
            t.actions.c.action_id > after,
        )
        if task_id is not None:
            query = query.where(t.actions.c.task_id == task_id)
        if state is not None:
            query = query.where(t.actions.c.state == state)
        rows = self.c.execute(query.order_by(t.actions.c.action_id).limit(limit)).mappings()
        return [dict(row) for row in rows]

    def state_counts(self, project_id):
        return {
            row["state"]: row["count"]
            for row in self.c.execute(
                sa.select(t.actions.c.state, sa.func.count().label("count"))
                .where(t.actions.c.project_id == project_id)
                .group_by(t.actions.c.state)
            ).mappings()
        }

    def get(self, action_id):
        return one(self.c, sa.select(t.actions).where(t.actions.c.action_id == action_id))

    def add(self, record):
        self.c.execute(t.actions.insert().values(**record))

    def update(self, action_id, **changes):
        allowed = {
            "send_started",
            "state",
            "external_revision",
            "execution_id",
            "stopped",
            "cancel_requested",
            "failure_count",
            "last_external_observation",
        }
        if not set(changes) <= allowed:
            raise ValueError("Frozen action fields cannot change")
        self.c.execute(
            t.actions.update().where(t.actions.c.action_id == action_id).values(**changes)
        )

    def active(self, task_id):
        return [
            dict(row)
            for row in self.c.execute(
                sa.select(t.actions).where(
                    t.actions.c.task_id == task_id,
                    t.actions.c.stopped.is_(False),
                )
            ).mappings()
        ]

    def command(self, project_id, command_id):
        return one(
            self.c,
            sa.select(t.commands).where(
                t.commands.c.project_id == project_id, t.commands.c.command_id == command_id
            ),
        )

    def add_command(self, record):
        self.c.execute(t.commands.insert().values(**record))

    def update_command(self, command_id, **changes):
        self.c.execute(
            t.commands.update().where(t.commands.c.command_id == command_id).values(**changes)
        )


class DeliveryRepository(Repository):
    @staticmethod
    def key(actor, scope, operation, key):
        return dict(principal_id=actor.principal_id, scope=scope, operation=operation, key=key)

    def replay(self, actor, scope, operation, key):
        identity = self.key(actor, scope, operation, key)
        record = one(
            self.c,
            sa.select(t.requests)
            .where(*[t.requests.c[name] == value for name, value in identity.items()])
            .with_for_update(),
            required=False,
        )
        return record["result"] if record else None

    def idempotency(self, actor, scope, operation, key, digest):
        identity = self.key(actor, scope, operation, key)
        self.c.execute(
            insert(t.requests).values(**identity, digest=digest).on_conflict_do_nothing()
        )
        query = (
            sa.select(t.requests)
            .where(*[t.requests.c[k] == v for k, v in identity.items()])
            .with_for_update()
        )
        record = one(self.c, query)
        require(
            record["digest"] == digest, "IDEMPOTENCY_CONFLICT", "Request key has different content"
        )
        return record["result"]

    def remember(self, actor, scope, operation, key, result):
        identity = self.key(actor, scope, operation, key)
        self.c.execute(
            t.requests.update()
            .where(*[t.requests.c[k] == v for k, v in identity.items()])
            .values(result=result)
        )

    def enqueue(self, event_id, task_id, kind, payload):
        self.c.execute(
            t.outbox.insert().values(event_id=event_id, task_id=task_id, kind=kind, payload=payload)
        )

    def claim(self, owner, lease_seconds):
        now = self.now()
        record = one(
            self.c,
            sa.select(t.outbox)
            .where(
                t.outbox.c.done.is_(False),
                t.outbox.c.available_at <= now,
                sa.or_(t.outbox.c.lease_until.is_(None), t.outbox.c.lease_until <= now),
            )
            .order_by(t.outbox.c.available_at, t.outbox.c.event_id)
            .with_for_update(skip_locked=True)
            .limit(1),
            required=False,
        )
        if record is None:
            return None
        changes = dict(
            owner=owner,
            lease_until=now + timedelta(seconds=lease_seconds),
            generation=record["generation"] + 1,
            attempts=record["attempts"] + 1,
        )
        self.c.execute(
            t.outbox.update().where(t.outbox.c.event_id == record["event_id"]).values(**changes)
        )
        return record | changes

    def predicate(self, event_id, owner, generation):
        return sa.and_(
            t.outbox.c.event_id == event_id,
            t.outbox.c.owner == owner,
            t.outbox.c.generation == generation,
            t.outbox.c.lease_until > sa.func.clock_timestamp(),
            t.outbox.c.done.is_(False),
        )

    def owned(self, event_id, owner, generation):
        record = one(
            self.c,
            sa.select(t.outbox).where(t.outbox.c.event_id == event_id).with_for_update(),
            required=False,
        )
        # Read database time after the row lock is acquired: waiting for a lock
        # must not turn a pre-wait timestamp into permission to use an expired lease.
        return bool(
            record
            and not record["done"]
            and record["owner"] == owner
            and record["generation"] == generation
            and record["lease_until"] is not None
            and record["lease_until"] > self.now()
        )

    def renew(self, event_id, owner, generation, seconds):
        return (
            self.c.execute(
                t.outbox.update()
                .where(self.predicate(event_id, owner, generation))
                .values(lease_until=self.now() + timedelta(seconds=seconds))
            ).rowcount
            == 1
        )

    def finish(self, event_id, owner, generation, delay=None):
        self.c.execute(
            t.outbox.update()
            .where(self.predicate(event_id, owner, generation))
            .values(
                done=delay is None,
                owner=None,
                lease_until=None,
                available_at=self.now() + timedelta(seconds=delay or 0),
            )
        )

    def inbox(self, source, event_id, digest):
        inserted = self.c.execute(
            insert(t.inbox)
            .values(source=source, event_id=event_id, digest=digest)
            .on_conflict_do_nothing()
            .returning(t.inbox.c.event_id)
        ).scalar_one_or_none()
        if inserted is not None:
            return True
        row = one(
            self.c,
            sa.select(t.inbox).where(t.inbox.c.source == source, t.inbox.c.event_id == event_id),
        )
        require(
            row["digest"] == digest,
            "INVALID_BINDING",
            "Event identity has conflicting content",
            422,
        )
        return False

    def audit(self, event_id, task_id, payload):
        self.c.execute(
            insert(t.audit)
            .values(event_id=event_id, task_id=task_id, payload=payload)
            .on_conflict_do_nothing()
        )


class ScienceRepository(Repository):
    def permit(self, action_id, execution_id, attempt_id):
        self.c.execute(
            insert(t.permits)
            .values(action_id=action_id, execution_id=execution_id, attempt_id=attempt_id)
            .on_conflict_do_nothing()
        )
        row = one(self.c, sa.select(t.permits).where(t.permits.c.action_id == action_id))
        require(
            row["execution_id"] == execution_id and row["attempt_id"] == attempt_id,
            "INVALID_BINDING",
            "Execution attempt changed",
        )

    def candidates(self, task_id):
        return [
            dict(row)
            for row in self.c.execute(
                sa.select(t.deliverables).where(t.deliverables.c.task_id == task_id)
            ).mappings()
        ]

    def candidate(self, project, id):
        return one(
            self.c,
            sa.select(t.deliverables).where(
                t.deliverables.c.project_id == project, t.deliverables.c.deliverable_id == id
            ),
        )

    def add_candidate(self, record):
        self.c.execute(t.deliverables.insert().values(**record))

    def checks(self, task_id):
        return [
            dict(row)
            for row in self.c.execute(
                sa.select(t.acceptances).where(t.acceptances.c.task_id == task_id)
            ).mappings()
        ]

    def check(self, project, id):
        return one(
            self.c,
            sa.select(t.acceptances).where(
                t.acceptances.c.project_id == project, t.acceptances.c.acceptance_id == id
            ),
        )

    def add_check(self, record):
        self.c.execute(t.acceptances.insert().values(**record))

    def set_primary_check_action(self, acceptance_id, action_id):
        self.c.execute(
            t.acceptances.update()
            .where(
                t.acceptances.c.acceptance_id == acceptance_id,
                t.acceptances.c.action_id.is_(None),
            )
            .values(action_id=action_id)
        )

    def pending_checks(self):
        query = (
            sa.select(t.acceptances)
            .join(t.actions, t.actions.c.action_id == t.acceptances.c.action_id)
            .join(t.tasks, t.tasks.c.task_id == t.acceptances.c.task_id)
            .where(
                ~sa.exists(
                    sa.select(t.check_envelopes.c.acceptance_id).where(
                        t.check_envelopes.c.acceptance_id == t.acceptances.c.acceptance_id
                    )
                ),
                t.actions.c.stopped.is_(True),
                sa.or_(
                    t.acceptances.c.status == "QUEUED",
                    sa.and_(
                        t.acceptances.c.status == "PASS",
                        t.tasks.c.lifecycle == "OPEN",
                        t.tasks.c.control == "ACTIVE",
                        t.tasks.c.spec_revision == t.acceptances.c.spec_revision,
                    ),
                ),
            )
        )
        return [
            dict(row)
            for row in self.c.execute(
                query.order_by(t.acceptances.c.acceptance_id).limit(100)
            ).mappings()
        ]

    def report(self, id, status, report, digest):
        self.c.execute(
            t.acceptances.update()
            .where(t.acceptances.c.acceptance_id == id, t.acceptances.c.status == "QUEUED")
            .values(status=status, report=report, report_digest=digest)
        )

    def complete(self, task, acceptance):
        self.c.execute(
            t.completions.insert().values(
                task_id=task.task_id,
                spec_revision=task.spec_revision,
                acceptance_id=acceptance["acceptance_id"],
                report_digest=acceptance["report_digest"],
            )
        )

    def completion(self, task_id):
        return one(
            self.c,
            sa.select(t.completions).where(t.completions.c.task_id == task_id),
            required=False,
        )

    def add_plan(self, plan, items, evidence):
        self.c.execute(t.coverage_plans.insert().values(**plan))
        if items:
            self.c.execute(t.coverage_items.insert(), items)
        if evidence:
            self.c.execute(t.acceptance_evidence.insert(), evidence)

    def add_check_envelope(self, record):
        self.c.execute(t.check_envelopes.insert().values(**record))

    def check_envelope(self, acceptance_id, *, lock=False):
        query = sa.select(t.check_envelopes).where(
            t.check_envelopes.c.acceptance_id == acceptance_id
        )
        if lock:
            query = query.with_for_update()
        return one(self.c, query, required=False)

    def add_check_runs(self, rows):
        if rows:
            self.c.execute(t.check_runs.insert(), rows)

    def check_runs(self, acceptance_id, *, lock=False):
        query = (
            sa.select(t.check_runs)
            .where(t.check_runs.c.acceptance_id == acceptance_id)
            .order_by(t.check_runs.c.run_key)
        )
        if lock:
            query = query.with_for_update()
        return [dict(row) for row in self.c.execute(query).mappings()]

    def check_run(self, run_id, *, lock=False):
        query = sa.select(t.check_runs).where(t.check_runs.c.run_id == run_id)
        if lock:
            query = query.with_for_update()
        return one(self.c, query)

    def bind_check_run(self, run_id, action_id):
        run = self.check_run(run_id)
        plan = one(
            self.c,
            sa.select(t.coverage_plans.c.plan_id).where(
                t.coverage_plans.c.acceptance_id == run["acceptance_id"]
            ),
        )
        changed = self.c.execute(
            t.check_runs.update()
            .where(
                t.check_runs.c.run_id == run_id,
                t.check_runs.c.status == "PLANNED",
                t.check_runs.c.action_id.is_(None),
            )
            .values(action_id=action_id, status="QUEUED")
        ).rowcount
        require(changed == 1, "REVISION_CONFLICT", "Checker run was already admitted")
        self.c.execute(
            t.coverage_items.update()
            .where(
                t.coverage_items.c.plan_id == plan["plan_id"],
                t.coverage_items.c.capability_id == run["capability_id"],
            )
            .values(check_run_id=run_id)
        )

    def finish_check_run(self, run_id, status, report):
        changed = self.c.execute(
            t.check_runs.update()
            .where(
                t.check_runs.c.run_id == run_id,
                t.check_runs.c.status == "QUEUED",
            )
            .values(status=status, report=report, report_digest=digest_record(report))
        ).rowcount
        require(changed == 1, "REVISION_CONFLICT", "Checker run is no longer pending")

    def skip_check_run(self, run_id, status, report):
        changed = self.c.execute(
            t.check_runs.update()
            .where(
                t.check_runs.c.run_id == run_id,
                t.check_runs.c.status == "PLANNED",
                t.check_runs.c.action_id.is_(None),
            )
            .values(status=status, report=report, report_digest=digest_record(report))
        ).rowcount
        require(changed == 1, "REVISION_CONFLICT", "Checker run is no longer planned")

    def active_r2_checks(self):
        return [
            dict(row)
            for row in self.c.execute(
                sa.select(t.acceptances)
                .join(
                    t.check_envelopes,
                    t.check_envelopes.c.acceptance_id == t.acceptances.c.acceptance_id,
                )
                .join(t.tasks, t.tasks.c.task_id == t.acceptances.c.task_id)
                .where(
                    sa.or_(
                        t.acceptances.c.status == "QUEUED",
                        sa.and_(
                            t.acceptances.c.status == "STALE",
                            t.check_envelopes.c.released.is_(False),
                        ),
                        sa.and_(
                            t.acceptances.c.status == "PASS",
                            t.tasks.c.lifecycle == "OPEN",
                            t.tasks.c.control == "ACTIVE",
                            t.tasks.c.spec_revision == t.acceptances.c.spec_revision,
                        ),
                    )
                )
                .order_by(t.acceptances.c.acceptance_id)
                .limit(100)
            ).mappings()
        ]

    def release_check_envelope(self, acceptance_id):
        envelope = self.check_envelope(acceptance_id, lock=True)
        if envelope is None or envelope["released"]:
            return 0
        runs = self.check_runs(acceptance_id, lock=True)
        require(
            all(row["status"] not in {"PLANNED", "QUEUED"} for row in runs),
            "PRECONDITION_UNRESOLVED",
            "Checker runs have not converged",
        )
        started = sum(row["action_id"] is not None for row in runs)
        require(started <= envelope["amount"], "INVALID_BINDING", "Checker envelope exceeded")
        self.c.execute(
            t.check_envelopes.update()
            .where(t.check_envelopes.c.acceptance_id == acceptance_id)
            .values(released=True)
        )
        return envelope["amount"] - started

    def plan(self, acceptance_id):
        row = one(
            self.c,
            sa.select(t.coverage_plans).where(t.coverage_plans.c.acceptance_id == acceptance_id),
            required=False,
        )
        if row is None:
            return None
        row["items"] = [
            dict(item)
            for item in self.c.execute(
                sa.select(t.coverage_items)
                .where(t.coverage_items.c.plan_id == row["plan_id"])
                .order_by(t.coverage_items.c.ordinal)
            ).mappings()
        ]
        return row

    def apply_item_results(self, plan_id, results):
        for result in results:
            changed = self.c.execute(
                t.coverage_items.update()
                .where(
                    t.coverage_items.c.plan_id == plan_id,
                    t.coverage_items.c.item_id == result["item_id"],
                    t.coverage_items.c.automatic.is_(True),
                )
                .values(status=result["status"], result=result)
            ).rowcount
            require(changed == 1, "INVALID_BINDING", "Checker returned an unknown coverage item")

    def update_check(self, acceptance_id, status, report, report_digest):
        self.c.execute(
            t.acceptances.update()
            .where(t.acceptances.c.acceptance_id == acceptance_id)
            .values(status=status, report=report, report_digest=report_digest)
        )

    def mark_plan_stale(self, acceptance_id):
        plan = one(
            self.c,
            sa.select(t.coverage_plans).where(t.coverage_plans.c.acceptance_id == acceptance_id),
            required=False,
        )
        if plan is None:
            return
        self.c.execute(
            t.coverage_plans.update()
            .where(t.coverage_plans.c.plan_id == plan["plan_id"])
            .values(status="STALE")
        )
        self.c.execute(
            t.coverage_items.update()
            .where(t.coverage_items.c.plan_id == plan["plan_id"])
            .values(status="STALE")
        )
        # The checker report, when present, remains an immutable historical fact.
        self.c.execute(
            t.acceptances.update()
            .where(t.acceptances.c.acceptance_id == acceptance_id)
            .values(status="STALE")
        )

    def add_review(self, record):
        self.c.execute(t.human_reviews.insert().values(**record))
        self.c.execute(
            t.coverage_items.update()
            .where(
                t.coverage_items.c.plan_id == record["plan_id"],
                t.coverage_items.c.item_id == record["item_id"],
                t.coverage_items.c.check_type == "HUMAN_REVIEW",
                t.coverage_items.c.status == "REVIEW_REQUIRED",
            )
            .values(
                status=record["decision"],
                result={
                    "item_id": record["item_id"],
                    "status": record["decision"],
                    "review_id": record["review_id"],
                },
            )
        )

    def reviews(self, acceptance_id):
        return [
            dict(row)
            for row in self.c.execute(
                sa.select(t.human_reviews)
                .where(t.human_reviews.c.acceptance_id == acceptance_id)
                .order_by(t.human_reviews.c.created_at)
            ).mappings()
        ]

    def set_completion_validity(self, task_id, validity, reason=None):
        current = one(
            self.c,
            sa.select(t.completion_validity).where(t.completion_validity.c.task_id == task_id),
            required=False,
        )
        reasons = list(current["reasons"] if current else [])
        if reason is not None and reason not in reasons:
            reasons.append(reason)
        statement = insert(t.completion_validity).values(
            task_id=task_id,
            validity=validity,
            revision=(current["revision"] + 1 if current else 1),
            reasons=reasons,
            updated_at=self.now(),
        )
        self.c.execute(
            statement.on_conflict_do_update(
                index_elements=[t.completion_validity.c.task_id],
                set_={
                    "validity": validity,
                    "revision": statement.excluded.revision,
                    "reasons": statement.excluded.reasons,
                    "updated_at": statement.excluded.updated_at,
                },
            )
        )

    def validity(self, task_id):
        return one(
            self.c,
            sa.select(t.completion_validity).where(t.completion_validity.c.task_id == task_id),
            required=False,
        )

    def apply_knowledge_event(self, event):
        cursor = one(
            self.c,
            sa.select(t.knowledge_lifecycle_cursors).where(
                t.knowledge_lifecycle_cursors.c.project_id == event["project_id"],
                t.knowledge_lifecycle_cursors.c.document_id == event["document_id"],
                t.knowledge_lifecycle_cursors.c.version_id == event["version_id"],
            ),
            required=False,
        )
        if cursor and cursor["revision"] >= event["revision"]:
            return []
        statement = insert(t.knowledge_lifecycle_cursors).values(
            project_id=event["project_id"],
            document_id=event["document_id"],
            version_id=event["version_id"],
            revision=event["revision"],
            event_id=event["event_id"],
        )
        self.c.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    t.knowledge_lifecycle_cursors.c.project_id,
                    t.knowledge_lifecycle_cursors.c.document_id,
                    t.knowledge_lifecycle_cursors.c.version_id,
                ],
                set_={"revision": event["revision"], "event_id": event["event_id"]},
            )
        )
        if event["kind"] != "WITHDRAWN":
            return []
        rows = [
            dict(row)
            for row in self.c.execute(
                sa.select(
                    t.acceptance_evidence.c.acceptance_id,
                    t.acceptances.c.task_id,
                    t.coverage_plans.c.plan_id,
                )
                .join(
                    t.acceptances,
                    t.acceptances.c.acceptance_id == t.acceptance_evidence.c.acceptance_id,
                )
                .join(
                    t.coverage_plans,
                    t.coverage_plans.c.acceptance_id == t.acceptances.c.acceptance_id,
                )
                .where(
                    t.acceptance_evidence.c.project_id == event["project_id"],
                    t.acceptance_evidence.c.document_id == event["document_id"],
                    t.acceptance_evidence.c.version_id == event["version_id"],
                )
            ).mappings()
        ]
        for row in rows:
            self.c.execute(
                t.coverage_plans.update()
                .where(t.coverage_plans.c.plan_id == row["plan_id"])
                .values(status="STALE")
            )
            self.c.execute(
                t.coverage_items.update()
                .where(t.coverage_items.c.plan_id == row["plan_id"])
                .values(status="STALE")
            )
            if self.completion(row["task_id"]):
                self.set_completion_validity(
                    row["task_id"],
                    "STALE",
                    {
                        "code": "EVIDENCE_WITHDRAWN",
                        "event_id": event["event_id"],
                        "document_id": event["document_id"],
                        "version_id": event["version_id"],
                    },
                )
        return rows


class ResearchRepository(Repository):
    def add_policy_decision(self, record):
        self.c.execute(t.policy_decisions.insert().values(**record))

    def policy_decision(self, decision_id):
        return one(
            self.c,
            sa.select(t.policy_decisions).where(t.policy_decisions.c.decision_id == decision_id),
        )

    def policy_decisions(self, task_id):
        return [
            dict(row)
            for row in self.c.execute(
                sa.select(t.policy_decisions)
                .where(t.policy_decisions.c.task_id == task_id)
                .order_by(t.policy_decisions.c.decision_id)
            ).mappings()
        ]

    def add_analysis(self, record):
        self.c.execute(t.analysis_revisions.insert().values(**record))

    def analysis(self, project, task_id, revision_id, *, lock=False):
        query = sa.select(t.analysis_revisions).where(
            t.analysis_revisions.c.project_id == project,
            t.analysis_revisions.c.task_id == task_id,
            t.analysis_revisions.c.revision_id == revision_id,
        )
        return one(self.c, query.with_for_update() if lock else query)

    def analyses(self, task_id):
        return [
            dict(row)
            for row in self.c.execute(
                sa.select(t.analysis_revisions)
                .where(t.analysis_revisions.c.task_id == task_id)
                .order_by(t.analysis_revisions.c.analysis_id, t.analysis_revisions.c.revision)
            ).mappings()
        ]

    def successor(self, revision_id):
        return one(
            self.c,
            sa.select(t.analysis_revisions).where(
                t.analysis_revisions.c.supersedes_revision_id == revision_id
            ),
            required=False,
        )

    def add_registration(self, record):
        self.c.execute(t.analysis_registrations.insert().values(**record))

    def registration_for_revision(self, revision_id):
        return one(
            self.c,
            sa.select(t.analysis_registrations).where(
                t.analysis_registrations.c.revision_id == revision_id
            ),
            required=False,
        )

    def registration(self, project, task_id, registration_id):
        return one(
            self.c,
            sa.select(t.analysis_registrations).where(
                t.analysis_registrations.c.project_id == project,
                t.analysis_registrations.c.task_id == task_id,
                t.analysis_registrations.c.registration_id == registration_id,
            ),
        )

    def registrations(self, task_id):
        return [
            dict(row)
            for row in self.c.execute(
                sa.select(t.analysis_registrations)
                .where(t.analysis_registrations.c.task_id == task_id)
                .order_by(t.analysis_registrations.c.registration_id)
            ).mappings()
        ]

    def add_claim(self, record):
        self.c.execute(t.claim_revisions.insert().values(**record))

    def claim_revision(self, project, task_id, claim_id, revision, *, lock=False):
        query = sa.select(t.claim_revisions).where(
            t.claim_revisions.c.project_id == project,
            t.claim_revisions.c.task_id == task_id,
            t.claim_revisions.c.claim_id == claim_id,
            t.claim_revisions.c.revision == revision,
        )
        return one(self.c, query.with_for_update() if lock else query)

    def claims(self, task_id):
        return [
            dict(row)
            for row in self.c.execute(
                sa.select(t.claim_revisions)
                .where(t.claim_revisions.c.task_id == task_id)
                .order_by(t.claim_revisions.c.claim_id, t.claim_revisions.c.revision)
            ).mappings()
        ]

    def add_provenance(self, record):
        self.c.execute(
            insert(t.provenance_relations)
            .values(**record)
            .on_conflict_do_nothing(index_elements=[t.provenance_relations.c.relation_digest])
        )

    def provenance(self, task_id):
        return [
            dict(row)
            for row in self.c.execute(
                sa.select(t.provenance_relations)
                .where(t.provenance_relations.c.task_id == task_id)
                .order_by(t.provenance_relations.c.relation_id)
            ).mappings()
        ]


class WorkflowRepository(Repository):
    def add_package(self, record):
        self.c.execute(t.workflow_packages.insert().values(**record))

    def package(self, project_id, package_id):
        return one(
            self.c,
            sa.select(t.workflow_packages).where(
                t.workflow_packages.c.project_id == project_id,
                t.workflow_packages.c.package_id == package_id,
            ),
        )

    def packages(self, project_id, after, limit):
        rows = self.c.execute(
            sa.select(t.workflow_packages)
            .where(
                t.workflow_packages.c.project_id == project_id,
                t.workflow_packages.c.package_id > after,
            )
            .order_by(t.workflow_packages.c.package_id)
            .limit(limit)
        ).mappings()
        return [dict(row) for row in rows]

    def claim_package(self, owner, lease_seconds):
        now = self.now()
        row = one(
            self.c,
            sa.select(t.workflow_packages)
            .where(
                t.workflow_packages.c.status.in_(["QUEUED", "VALIDATING"]),
                sa.or_(
                    t.workflow_packages.c.lease_until.is_(None),
                    t.workflow_packages.c.lease_until <= now,
                ),
            )
            .order_by(t.workflow_packages.c.package_id)
            .with_for_update(skip_locked=True)
            .limit(1),
            required=False,
        )
        if row is None:
            return None
        changes = {
            "status": "VALIDATING",
            "owner": owner,
            "generation": row["generation"] + 1,
            "lease_until": now + timedelta(seconds=lease_seconds),
        }
        self.c.execute(
            t.workflow_packages.update()
            .where(t.workflow_packages.c.package_id == row["package_id"])
            .values(**changes)
        )
        return row | changes

    def finish_package(self, package_id, owner, generation, *, status, compiled, error):
        require(status in {"READY", "REJECTED"}, "INVALID_BINDING", "Invalid package state")
        return (
            self.c.execute(
                t.workflow_packages.update()
                .where(
                    t.workflow_packages.c.package_id == package_id,
                    t.workflow_packages.c.owner == owner,
                    t.workflow_packages.c.generation == generation,
                    t.workflow_packages.c.status == "VALIDATING",
                    t.workflow_packages.c.lease_until > sa.func.clock_timestamp(),
                )
                .values(
                    status=status,
                    compiled=compiled,
                    error=error,
                    owner=None,
                    lease_until=None,
                )
            ).rowcount
            == 1
        )

    def add_envelope(self, action_id, amount):
        self.c.execute(t.workflow_envelopes.insert().values(action_id=action_id, amount=amount))

    def envelope(self, action_id, *, required=True):
        return one(
            self.c,
            sa.select(t.workflow_envelopes).where(t.workflow_envelopes.c.action_id == action_id),
            required=required,
        )

    def release_envelope(self, action_id):
        envelope = one(
            self.c,
            sa.select(t.workflow_envelopes)
            .where(t.workflow_envelopes.c.action_id == action_id)
            .with_for_update(),
        )
        if envelope["released"]:
            return 0
        rows = self.nodes(action_id)
        require(
            all(not row["slot_acquired"] or row["stopped"] for row in rows),
            "PRECONDITION_UNRESOLVED",
            "Workflow nodes have not converged",
        )
        used = sum(bool(row["executed"]) for row in rows)
        require(used <= envelope["amount"], "INVALID_BINDING", "Workflow envelope exceeded")
        self.c.execute(
            t.workflow_envelopes.update()
            .where(t.workflow_envelopes.c.action_id == action_id)
            .values(released=True)
        )
        return envelope["amount"] - used

    def node(self, node_id, *, lock=False):
        query = sa.select(t.workflow_nodes).where(t.workflow_nodes.c.node_id == node_id)
        if lock:
            query = query.with_for_update()
        return one(self.c, query, required=False)

    def add_node(self, record):
        self.c.execute(t.workflow_nodes.insert().values(**record))

    def nodes(self, action_id, after="", limit=1001):
        rows = self.c.execute(
            sa.select(t.workflow_nodes)
            .where(
                t.workflow_nodes.c.action_id == action_id,
                t.workflow_nodes.c.node_id > after,
            )
            .order_by(t.workflow_nodes.c.node_id)
            .limit(limit)
        ).mappings()
        return [dict(row) for row in rows]

    def update_node(self, node_id, **changes):
        allowed = {"slot_acquired", "executed", "stopped", "revision", "observation"}
        if not set(changes) <= allowed:
            raise ValueError("Frozen workflow node fields cannot change")
        self.c.execute(
            t.workflow_nodes.update().where(t.workflow_nodes.c.node_id == node_id).values(**changes)
        )


class AuthoringRepository(Repository):
    def workspace_counts(self, project_id):
        return {
            "workflow_definitions": self.c.scalar(
                sa.select(sa.func.count())
                .select_from(t.workflow_definitions)
                .where(t.workflow_definitions.c.project_id == project_id)
            ),
            "workflow_releases": self.c.scalar(
                sa.select(sa.func.count())
                .select_from(t.workflow_releases)
                .where(t.workflow_releases.c.project_id == project_id)
            ),
            "operator_proposals": self.c.scalar(
                sa.select(sa.func.count())
                .select_from(t.operator_proposals)
                .where(t.operator_proposals.c.project_id == project_id)
            ),
        }

    def add_definition(self, definition, revision):
        inserted = self.c.execute(
            insert(t.workflow_definitions)
            .values(**definition)
            .on_conflict_do_nothing(
                index_elements=[
                    t.workflow_definitions.c.project_id,
                    t.workflow_definitions.c.stable_key,
                ]
            )
            .returning(t.workflow_definitions.c.definition_id)
        ).scalar_one_or_none()
        require(
            inserted is not None,
            "REVISION_CONFLICT",
            "Workflow stable key already exists",
            409,
        )
        self.c.execute(t.workflow_revisions.insert().values(**revision))

    def definition(self, project_id, definition_id, *, lock=False):
        query = sa.select(t.workflow_definitions).where(
            t.workflow_definitions.c.project_id == project_id,
            t.workflow_definitions.c.definition_id == definition_id,
        )
        return one(self.c, query.with_for_update() if lock else query)

    def definitions(self, project_id, after, limit):
        rows = self.c.execute(
            sa.select(t.workflow_definitions)
            .where(
                t.workflow_definitions.c.project_id == project_id,
                t.workflow_definitions.c.definition_id > after,
            )
            .order_by(t.workflow_definitions.c.definition_id)
            .limit(limit)
        ).mappings()
        return [dict(row) for row in rows]

    def add_revision(self, project_id, definition_id, revision):
        updated = self.c.execute(
            t.workflow_definitions.update()
            .where(
                t.workflow_definitions.c.project_id == project_id,
                t.workflow_definitions.c.definition_id == definition_id,
                t.workflow_definitions.c.head_revision == revision["revision"] - 1,
            )
            .values(head_revision=revision["revision"])
        )
        require(updated.rowcount == 1, "REVISION_CONFLICT", "Workflow draft head changed", 409)
        self.c.execute(t.workflow_revisions.insert().values(**revision))

    def revision(self, project_id, definition_id, revision):
        return one(
            self.c,
            sa.select(t.workflow_revisions).where(
                t.workflow_revisions.c.project_id == project_id,
                t.workflow_revisions.c.definition_id == definition_id,
                t.workflow_revisions.c.revision == revision,
            ),
        )

    def revisions(self, project_id, definition_id, after, limit):
        rows = self.c.execute(
            sa.select(t.workflow_revisions)
            .where(
                t.workflow_revisions.c.project_id == project_id,
                t.workflow_revisions.c.definition_id == definition_id,
                t.workflow_revisions.c.revision > after,
            )
            .order_by(t.workflow_revisions.c.revision)
            .limit(limit)
        ).mappings()
        return [dict(row) for row in rows]

    def add_validation(self, record):
        self.c.execute(t.authoring_validations.insert().values(**record))

    def validation(self, project_id, validation_id):
        return one(
            self.c,
            sa.select(t.authoring_validations).where(
                t.authoring_validations.c.project_id == project_id,
                t.authoring_validations.c.validation_id == validation_id,
            ),
        )

    def claim_validation(self, owner, lease_seconds):
        now = self.now()
        row = one(
            self.c,
            sa.select(t.authoring_validations)
            .where(
                t.authoring_validations.c.status.in_(["QUEUED", "RUNNING"]),
                sa.or_(
                    t.authoring_validations.c.lease_until.is_(None),
                    t.authoring_validations.c.lease_until <= now,
                ),
            )
            .order_by(t.authoring_validations.c.created_at)
            .with_for_update(skip_locked=True)
            .limit(1),
            required=False,
        )
        if row is None:
            return None
        changes = {
            "status": "RUNNING",
            "owner": owner,
            "generation": row["generation"] + 1,
            "lease_until": now + timedelta(seconds=lease_seconds),
            "started_at": row["started_at"] or now,
        }
        self.c.execute(
            t.authoring_validations.update()
            .where(t.authoring_validations.c.validation_id == row["validation_id"])
            .values(**changes)
        )
        return row | changes

    def finish_validation(self, validation_id, owner, generation, *, status, issues):
        require(
            status in {"PASSED", "REJECTED", "ERROR"},
            "INVALID_BINDING",
            "Invalid authoring validation state",
        )
        return (
            self.c.execute(
                t.authoring_validations.update()
                .where(
                    t.authoring_validations.c.validation_id == validation_id,
                    t.authoring_validations.c.owner == owner,
                    t.authoring_validations.c.generation == generation,
                    t.authoring_validations.c.status == "RUNNING",
                    t.authoring_validations.c.lease_until > sa.func.clock_timestamp(),
                )
                .values(
                    status=status,
                    issues=issues,
                    owner=None,
                    lease_until=None,
                    finished_at=sa.func.clock_timestamp(),
                )
            ).rowcount
            == 1
        )

    def add_candidate(self, record):
        self.c.execute(t.workflow_candidates.insert().values(**record))

    def candidate(self, project_id, definition_id, candidate_id):
        return one(
            self.c,
            sa.select(t.workflow_candidates).where(
                t.workflow_candidates.c.project_id == project_id,
                t.workflow_candidates.c.definition_id == definition_id,
                t.workflow_candidates.c.candidate_id == candidate_id,
            ),
        )

    def candidate_by_package(self, package_id):
        return one(
            self.c,
            sa.select(t.workflow_candidates).where(
                t.workflow_candidates.c.package_id == package_id
            ),
            required=False,
        )

    def candidates(self, project_id, definition_id, after, limit):
        rows = self.c.execute(
            sa.select(t.workflow_candidates)
            .where(
                t.workflow_candidates.c.project_id == project_id,
                t.workflow_candidates.c.definition_id == definition_id,
                t.workflow_candidates.c.candidate_id > after,
            )
            .order_by(t.workflow_candidates.c.candidate_id)
            .limit(limit)
        ).mappings()
        return [dict(row) for row in rows]

    def add_trial(self, record):
        self.c.execute(t.workflow_trials.insert().values(**record))

    def trial(self, project_id, definition_id, trial_id):
        return one(
            self.c,
            sa.select(t.workflow_trials).where(
                t.workflow_trials.c.project_id == project_id,
                t.workflow_trials.c.definition_id == definition_id,
                t.workflow_trials.c.trial_id == trial_id,
            ),
        )

    def trials(self, project_id, definition_id, after, limit):
        rows = self.c.execute(
            sa.select(t.workflow_trials)
            .where(
                t.workflow_trials.c.project_id == project_id,
                t.workflow_trials.c.definition_id == definition_id,
                t.workflow_trials.c.trial_id > after,
            )
            .order_by(t.workflow_trials.c.trial_id)
            .limit(limit)
        ).mappings()
        return [dict(row) for row in rows]

    def claim_trial(self, owner, lease_seconds):
        now = self.now()
        row = one(
            self.c,
            sa.select(t.workflow_trials)
            .where(
                sa.or_(
                    sa.and_(
                        t.workflow_trials.c.case_verdict == "PENDING",
                        sa.or_(
                            t.workflow_trials.c.next_retry_at.is_(None),
                            t.workflow_trials.c.next_retry_at <= now,
                        ),
                    ),
                    sa.and_(
                        t.workflow_trials.c.case_verdict == "ERROR",
                        t.workflow_trials.c.next_retry_at.is_not(None),
                        t.workflow_trials.c.next_retry_at <= now,
                    ),
                ),
                sa.or_(
                    t.workflow_trials.c.lease_until.is_(None),
                    t.workflow_trials.c.lease_until <= now,
                ),
            )
            .order_by(t.workflow_trials.c.created_at)
            .with_for_update(skip_locked=True)
            .limit(1),
            required=False,
        )
        if row is None:
            return None
        changes = {
            "owner": owner,
            "generation": row["generation"] + 1,
            "lease_until": now + timedelta(seconds=lease_seconds),
            "attempt": row["attempt"] + 1,
        }
        self.c.execute(
            t.workflow_trials.update()
            .where(t.workflow_trials.c.trial_id == row["trial_id"])
            .values(**changes)
        )
        return row | changes

    def finish_trial(self, trial_id, owner, generation, **changes):
        allowed = {
            "case_verdict",
            "case_result",
            "case_result_digest",
            "error_category",
            "next_retry_at",
            "finished_at",
        }
        if not set(changes) <= allowed:
            raise ValueError("Invalid workflow trial update")
        changes |= {"owner": None, "lease_until": None}
        return (
            self.c.execute(
                t.workflow_trials.update()
                .where(
                    t.workflow_trials.c.trial_id == trial_id,
                    t.workflow_trials.c.owner == owner,
                    t.workflow_trials.c.generation == generation,
                    t.workflow_trials.c.lease_until > sa.func.clock_timestamp(),
                    t.workflow_trials.c.case_verdict.in_(["PENDING", "ERROR"]),
                )
                .values(**changes)
            ).rowcount
            == 1
        )

    def add_release(self, record, trials):
        inserted = self.c.execute(
            insert(t.workflow_releases)
            .values(**record)
            .on_conflict_do_nothing(
                index_elements=[
                    t.workflow_releases.c.project_id,
                    t.workflow_releases.c.definition_id,
                    t.workflow_releases.c.version_label,
                ]
            )
            .returning(t.workflow_releases.c.release_id)
        ).scalar_one_or_none()
        require(inserted is not None, "REVISION_CONFLICT", "Workflow version already exists", 409)
        self.c.execute(
            t.workflow_release_lifecycle_events.insert().values(
                event_id=self.c.scalar(sa.select(sa.func.md5(record["release_id"] + ":initial"))),
                release_id=record["release_id"],
                lifecycle_revision=record.get("lifecycle_revision", 1),
                previous_lifecycle=None,
                lifecycle=record.get("lifecycle", "ACTIVE"),
                reason=record.get("lifecycle_reason", "INITIAL_RELEASE"),
                created_by=record["created_by"],
                created_at=record["created_at"],
            )
        )
        if trials:
            self.c.execute(t.workflow_release_trials.insert(), trials)

    def release(self, project_id, definition_id, version_label, *, lock=False):
        query = sa.select(t.workflow_releases).where(
            t.workflow_releases.c.project_id == project_id,
            t.workflow_releases.c.definition_id == definition_id,
            t.workflow_releases.c.version_label == version_label,
        )
        return one(
            self.c,
            query.with_for_update() if lock else query,
        )

    def releases(self, project_id, definition_id, after, limit):
        query = sa.select(t.workflow_releases).where(
            t.workflow_releases.c.project_id == project_id,
            t.workflow_releases.c.definition_id == definition_id,
        )
        if after:
            cursor = self.release(project_id, definition_id, after)
            query = query.where(
                sa.tuple_(
                    t.workflow_releases.c.version_sort,
                    t.workflow_releases.c.release_id,
                )
                > sa.tuple_(cursor["version_sort"], cursor["release_id"])
            )
        rows = self.c.execute(
            query.order_by(
                t.workflow_releases.c.version_sort,
                t.workflow_releases.c.release_id,
            ).limit(limit)
        ).mappings()
        return [dict(row) for row in rows]

    def release_by_id(self, project_id, release_id, *, lock=False):
        query = sa.select(t.workflow_releases).where(
            t.workflow_releases.c.project_id == project_id,
            t.workflow_releases.c.release_id == release_id,
        )
        return one(
            self.c,
            query.with_for_update() if lock else query,
        )

    def project_releases(self, project_id, after, limit):
        query = sa.select(t.workflow_releases).where(t.workflow_releases.c.project_id == project_id)
        if after:
            cursor = self.release_by_id(project_id, after)
            query = query.where(
                sa.tuple_(
                    t.workflow_releases.c.created_at,
                    t.workflow_releases.c.release_id,
                )
                > sa.tuple_(cursor["created_at"], cursor["release_id"])
            )
        rows = self.c.execute(
            query.order_by(
                t.workflow_releases.c.created_at,
                t.workflow_releases.c.release_id,
            ).limit(limit)
        ).mappings()
        return [dict(row) for row in rows]

    def release_trials(self, release_id):
        rows = self.c.execute(
            sa.select(t.workflow_release_trials)
            .where(t.workflow_release_trials.c.release_id == release_id)
            .order_by(t.workflow_release_trials.c.ordinal)
        ).mappings()
        return [dict(row) for row in rows]

    def release_lifecycle_events(self, release_id, after, limit):
        rows = self.c.execute(
            sa.select(t.workflow_release_lifecycle_events)
            .where(
                t.workflow_release_lifecycle_events.c.release_id == release_id,
                t.workflow_release_lifecycle_events.c.lifecycle_revision > after,
            )
            .order_by(t.workflow_release_lifecycle_events.c.lifecycle_revision)
            .limit(limit)
        ).mappings()
        return [dict(row) for row in rows]

    def transition_release(self, release_id, event):
        changed = self.c.execute(
            t.workflow_releases.update()
            .where(
                t.workflow_releases.c.release_id == release_id,
                t.workflow_releases.c.lifecycle_revision == event["lifecycle_revision"] - 1,
                t.workflow_releases.c.lifecycle == event["previous_lifecycle"],
            )
            .values(
                lifecycle=event["lifecycle"],
                lifecycle_revision=event["lifecycle_revision"],
                lifecycle_reason=event["reason"],
            )
        )
        require(changed.rowcount == 1, "VERSION_CONFLICT", "Release lifecycle changed", 409)
        self.c.execute(t.workflow_release_lifecycle_events.insert().values(**event))

    def release_publication_permit(self, action_id):
        return one(
            self.c,
            sa.select(t.workflow_release_publication_permits).where(
                t.workflow_release_publication_permits.c.action_id == action_id
            ),
            required=False,
        )

    def add_release_publication_permit(self, record):
        self.c.execute(t.workflow_release_publication_permits.insert().values(**record))

    def add_operator_proposal(self, proposal, revision):
        inserted = self.c.execute(
            insert(t.operator_proposals)
            .values(**proposal)
            .on_conflict_do_nothing(
                index_elements=[
                    t.operator_proposals.c.project_id,
                    t.operator_proposals.c.stable_key,
                ]
            )
            .returning(t.operator_proposals.c.proposal_id)
        ).scalar_one_or_none()
        require(
            inserted is not None,
            "REVISION_CONFLICT",
            "Operator proposal stable key already exists",
            409,
        )
        self.c.execute(t.operator_proposal_revisions.insert().values(**revision))

    def operator_proposal(self, project_id, proposal_id, *, lock=False):
        query = sa.select(t.operator_proposals).where(
            t.operator_proposals.c.project_id == project_id,
            t.operator_proposals.c.proposal_id == proposal_id,
        )
        return one(self.c, query.with_for_update() if lock else query)

    def operator_proposals(self, project_id, after, limit):
        rows = self.c.execute(
            sa.select(t.operator_proposals)
            .where(
                t.operator_proposals.c.project_id == project_id,
                t.operator_proposals.c.proposal_id > after,
            )
            .order_by(t.operator_proposals.c.proposal_id)
            .limit(limit)
        ).mappings()
        return [dict(row) for row in rows]

    def add_operator_proposal_revision(self, project_id, proposal_id, revision):
        updated = self.c.execute(
            t.operator_proposals.update()
            .where(
                t.operator_proposals.c.project_id == project_id,
                t.operator_proposals.c.proposal_id == proposal_id,
                t.operator_proposals.c.head_revision == revision["revision"] - 1,
            )
            .values(head_revision=revision["revision"])
        )
        require(updated.rowcount == 1, "REVISION_CONFLICT", "Operator proposal head changed", 409)
        self.c.execute(t.operator_proposal_revisions.insert().values(**revision))

    def operator_proposal_revision(self, project_id, proposal_id, revision):
        return one(
            self.c,
            sa.select(t.operator_proposal_revisions).where(
                t.operator_proposal_revisions.c.project_id == project_id,
                t.operator_proposal_revisions.c.proposal_id == proposal_id,
                t.operator_proposal_revisions.c.revision == revision,
            ),
        )

    def operator_proposal_revisions(self, project_id, proposal_id):
        rows = self.c.execute(
            sa.select(t.operator_proposal_revisions)
            .where(
                t.operator_proposal_revisions.c.project_id == project_id,
                t.operator_proposal_revisions.c.proposal_id == proposal_id,
            )
            .order_by(t.operator_proposal_revisions.c.revision)
        ).mappings()
        return [dict(row) for row in rows]

    def add_operator_review_event(self, record):
        self.c.execute(t.operator_review_events.insert().values(**record))

    def operator_review_events(self, project_id, proposal_id):
        rows = self.c.execute(
            sa.select(t.operator_review_events)
            .where(
                t.operator_review_events.c.project_id == project_id,
                t.operator_review_events.c.proposal_id == proposal_id,
            )
            .order_by(t.operator_review_events.c.created_at, t.operator_review_events.c.event_id)
        ).mappings()
        return [dict(row) for row in rows]

    def add_operator_deployment_receipt(self, record):
        inserted = self.c.execute(
            insert(t.operator_deployment_receipts)
            .values(**record)
            .on_conflict_do_nothing(constraint="uq_operator_deployment_evidence")
            .returning(t.operator_deployment_receipts.c.receipt_id)
        ).scalar_one_or_none()
        require(
            inserted is not None,
            "REVISION_CONFLICT",
            "Deployment evidence was already imported",
            409,
        )

    def operator_deployment_receipts(self, project_id, proposal_id):
        rows = self.c.execute(
            sa.select(t.operator_deployment_receipts)
            .where(
                t.operator_deployment_receipts.c.project_id == project_id,
                t.operator_deployment_receipts.c.proposal_id == proposal_id,
            )
            .order_by(
                t.operator_deployment_receipts.c.created_at,
                t.operator_deployment_receipts.c.receipt_id,
            )
        ).mappings()
        return [dict(row) for row in rows]


class AgentRepository(Repository):
    """Platform-owned Agent facts and leases; no framework checkpoint access."""

    def add_run(self, record):
        self.c.execute(t.agent_runs.insert().values(**record))

    def run(self, project_id, agent_run_id, *, lock=False, required=True):
        query = sa.select(t.agent_runs).where(
            t.agent_runs.c.project_id == project_id,
            t.agent_runs.c.agent_run_id == agent_run_id,
        )
        return one(self.c, query.with_for_update() if lock else query, required=required)

    def run_by_task(self, task_id, *, lock=False, required=False):
        query = sa.select(t.agent_runs).where(t.agent_runs.c.task_id == task_id)
        return one(self.c, query.with_for_update() if lock else query, required=required)

    def run_by_id(self, agent_run_id, *, lock=False):
        query = sa.select(t.agent_runs).where(t.agent_runs.c.agent_run_id == agent_run_id)
        return one(self.c, query.with_for_update() if lock else query)

    def update_run(self, agent_run_id, **changes):
        allowed = {
            "status",
            "epoch",
            "generation",
            "runtime_ref",
            "task_spec_revision",
            "control_version",
            "plan_revision",
            "frame_sequence",
            "accepted_proposals",
            "wake_revision",
            "blocked_reason",
            "updated_at",
        }
        if not set(changes) <= allowed:
            raise ValueError("Frozen AgentRun fields cannot change")
        self.c.execute(
            t.agent_runs.update()
            .where(t.agent_runs.c.agent_run_id == agent_run_id)
            .values(**changes)
        )

    def plan(self, agent_run_id, revision=None):
        query = sa.select(t.agent_plan_revisions).where(
            t.agent_plan_revisions.c.agent_run_id == agent_run_id
        )
        if revision is not None:
            query = query.where(t.agent_plan_revisions.c.revision == revision)
        else:
            query = query.order_by(t.agent_plan_revisions.c.revision.desc()).limit(1)
        return one(self.c, query, required=False)

    def plans(self, agent_run_id):
        return [
            dict(row)
            for row in self.c.execute(
                sa.select(t.agent_plan_revisions)
                .where(t.agent_plan_revisions.c.agent_run_id == agent_run_id)
                .order_by(t.agent_plan_revisions.c.revision)
            ).mappings()
        ]

    def add_plan(self, record):
        self.c.execute(t.agent_plan_revisions.insert().values(**record))

    def claim(self, owner, lease_seconds):
        now = self.now()
        row = one(
            self.c,
            sa.select(t.agent_runs)
            .join(t.tasks, t.tasks.c.task_id == t.agent_runs.c.task_id)
            .outerjoin(t.agent_leases, t.agent_leases.c.agent_run_id == t.agent_runs.c.agent_run_id)
            .where(
                t.agent_runs.c.status.in_(["READY", "ADVANCING"]),
                t.tasks.c.lifecycle.in_(["OPEN", "COMPLETED"]),
                t.tasks.c.control == "ACTIVE",
                sa.or_(
                    t.agent_leases.c.agent_run_id.is_(None),
                    t.agent_leases.c.lease_until <= now,
                ),
            )
            .order_by(t.agent_runs.c.updated_at, t.agent_runs.c.agent_run_id)
            .with_for_update(of=t.agent_runs, skip_locked=True)
            .limit(1),
            required=False,
        )
        if row is None:
            return None
        generation = row["generation"] + 1
        lease_until = now + timedelta(seconds=lease_seconds)
        self.c.execute(
            insert(t.agent_leases)
            .values(
                agent_run_id=row["agent_run_id"],
                owner=owner,
                generation=generation,
                lease_until=lease_until,
                acquired_at=now,
            )
            .on_conflict_do_update(
                index_elements=[t.agent_leases.c.agent_run_id],
                set_={
                    "owner": owner,
                    "generation": generation,
                    "lease_until": lease_until,
                    "acquired_at": now,
                },
            )
        )
        self.update_run(
            row["agent_run_id"],
            status="ADVANCING",
            generation=generation,
            updated_at=now,
        )
        return row | {
            "status": "ADVANCING",
            "generation": generation,
            "owner": owner,
            "lease_until": lease_until,
        }

    def lease_owned(self, agent_run_id, owner, generation):
        row = one(
            self.c,
            sa.select(t.agent_leases)
            .where(t.agent_leases.c.agent_run_id == agent_run_id)
            .with_for_update(),
            required=False,
        )
        return bool(
            row
            and row["owner"] == owner
            and row["generation"] == generation
            and row["lease_until"] > self.now()
        )

    def renew_lease(self, agent_run_id, owner, generation, lease_seconds):
        now = self.now()
        return (
            self.c.execute(
                t.agent_leases.update()
                .where(
                    t.agent_leases.c.agent_run_id == agent_run_id,
                    t.agent_leases.c.owner == owner,
                    t.agent_leases.c.generation == generation,
                    t.agent_leases.c.lease_until > now,
                )
                .values(lease_until=now + timedelta(seconds=lease_seconds))
            ).rowcount
            == 1
        )

    def release_lease(self, agent_run_id, owner, generation):
        self.c.execute(
            t.agent_leases.delete().where(
                t.agent_leases.c.agent_run_id == agent_run_id,
                t.agent_leases.c.owner == owner,
                t.agent_leases.c.generation == generation,
            )
        )

    def revoke_lease(self, agent_run_id):
        self.c.execute(t.agent_leases.delete().where(t.agent_leases.c.agent_run_id == agent_run_id))

    def active_lease(self, agent_run_id):
        row = one(
            self.c,
            sa.select(t.agent_leases).where(t.agent_leases.c.agent_run_id == agent_run_id),
            required=False,
        )
        if row is None or row["lease_until"] <= self.now():
            return None
        return row

    def add_context(self, record):
        self.c.execute(
            insert(t.agent_context_manifests)
            .values(**record)
            .on_conflict_do_nothing(
                index_elements=[
                    t.agent_context_manifests.c.agent_run_id,
                    t.agent_context_manifests.c.manifest_digest,
                ]
            )
        )
        return one(
            self.c,
            sa.select(t.agent_context_manifests).where(
                t.agent_context_manifests.c.agent_run_id == record["agent_run_id"],
                t.agent_context_manifests.c.manifest_digest == record["manifest_digest"],
            ),
        )

    def add_frame(self, record):
        self.c.execute(t.agent_decision_frames.insert().values(**record))

    def frame(self, frame_id, *, lock=False):
        query = sa.select(t.agent_decision_frames).where(
            t.agent_decision_frames.c.frame_id == frame_id
        )
        return one(self.c, query.with_for_update() if lock else query)

    def update_frame(self, frame_id, **changes):
        if not set(changes) <= {"state"}:
            raise ValueError("Frozen Agent frame fields cannot change")
        self.c.execute(
            t.agent_decision_frames.update()
            .where(t.agent_decision_frames.c.frame_id == frame_id)
            .values(**changes)
        )

    def frames(self, agent_run_id):
        return [
            dict(row)
            for row in self.c.execute(
                sa.select(t.agent_decision_frames)
                .where(t.agent_decision_frames.c.agent_run_id == agent_run_id)
                .order_by(t.agent_decision_frames.c.sequence)
            ).mappings()
        ]

    def stale_pending_frames(self, agent_run_id):
        self.c.execute(
            t.agent_decision_frames.update()
            .where(
                t.agent_decision_frames.c.agent_run_id == agent_run_id,
                t.agent_decision_frames.c.state == "PENDING",
            )
            .values(state="STALE")
        )

    def proposal(self, frame_id):
        return one(
            self.c,
            sa.select(t.agent_proposals).where(t.agent_proposals.c.frame_id == frame_id),
            required=False,
        )

    def proposal_by_id(self, proposal_id):
        return one(
            self.c,
            sa.select(t.agent_proposals).where(t.agent_proposals.c.proposal_id == proposal_id),
        )

    def add_proposal(self, record):
        self.c.execute(t.agent_proposals.insert().values(**record))

    def proposals(self, agent_run_id):
        return [
            dict(row)
            for row in self.c.execute(
                sa.select(t.agent_proposals)
                .where(t.agent_proposals.c.agent_run_id == agent_run_id)
                .order_by(t.agent_proposals.c.accepted_at, t.agent_proposals.c.proposal_id)
            ).mappings()
        ]

    def add_question(self, record):
        self.c.execute(t.agent_questions.insert().values(**record))

    def question(self, agent_run_id, question_id, *, lock=False):
        query = sa.select(t.agent_questions).where(
            t.agent_questions.c.agent_run_id == agent_run_id,
            t.agent_questions.c.question_id == question_id,
        )
        return one(self.c, query.with_for_update() if lock else query)

    def cancel_questions(self, agent_run_id):
        self.c.execute(
            t.agent_questions.update()
            .where(
                t.agent_questions.c.agent_run_id == agent_run_id,
                t.agent_questions.c.status == "OPEN",
            )
            .values(status="CANCELLED")
        )

    def questions(self, agent_run_id):
        return [
            dict(row)
            for row in self.c.execute(
                sa.select(t.agent_questions)
                .where(t.agent_questions.c.agent_run_id == agent_run_id)
                .order_by(t.agent_questions.c.created_at, t.agent_questions.c.question_id)
            ).mappings()
        ]

    def add_answer(self, record):
        self.c.execute(t.agent_answers.insert().values(**record))
        self.c.execute(
            t.agent_questions.update()
            .where(t.agent_questions.c.question_id == record["question_id"])
            .values(status="ANSWERED")
        )

    def answer(self, question_id):
        return one(
            self.c,
            sa.select(t.agent_answers).where(t.agent_answers.c.question_id == question_id),
            required=False,
        )

    def answers(self, agent_run_id):
        return [
            dict(row)
            for row in self.c.execute(
                sa.select(t.agent_answers)
                .where(t.agent_answers.c.agent_run_id == agent_run_id)
                .order_by(t.agent_answers.c.answered_at, t.agent_answers.c.answer_id)
            ).mappings()
        ]

    def add_wakeup(self, record):
        self.c.execute(t.agent_wakeups.insert().values(**record))

    def pending_wakeups(self, agent_run_id, *, lock=False):
        query = (
            sa.select(t.agent_wakeups)
            .where(
                t.agent_wakeups.c.agent_run_id == agent_run_id,
                t.agent_wakeups.c.state == "PENDING",
            )
            .order_by(t.agent_wakeups.c.revision)
        )
        if lock:
            query = query.with_for_update()
        return [dict(row) for row in self.c.execute(query).mappings()]

    def wakeups(self, agent_run_id):
        return [
            dict(row)
            for row in self.c.execute(
                sa.select(t.agent_wakeups)
                .where(t.agent_wakeups.c.agent_run_id == agent_run_id)
                .order_by(t.agent_wakeups.c.revision)
            ).mappings()
        ]

    def consume_wakeups(self, wake_ids, consumed_at):
        if wake_ids:
            self.c.execute(
                t.agent_wakeups.update()
                .where(t.agent_wakeups.c.wake_id.in_(wake_ids))
                .values(state="CONSUMED", consumed_at=consumed_at)
            )

    def cancel_wakeups(self, agent_run_id):
        self.c.execute(
            t.agent_wakeups.update()
            .where(
                t.agent_wakeups.c.agent_run_id == agent_run_id,
                t.agent_wakeups.c.state == "PENDING",
            )
            .values(state="CANCELLED")
        )

    def register_event(self, record):
        inserted = self.c.execute(
            insert(t.agent_inbox)
            .values(
                source=record["source"],
                event_id=record["event_id"],
                event_digest=record["event_digest"],
                observed_at=record["observed_at"],
            )
            .on_conflict_do_nothing()
            .returning(t.agent_inbox.c.event_id)
        ).scalar_one_or_none()
        if inserted is None:
            existing = one(
                self.c,
                sa.select(t.agent_inbox).where(
                    t.agent_inbox.c.source == record["source"],
                    t.agent_inbox.c.event_id == record["event_id"],
                ),
            )
            require(
                existing["event_digest"] == record["event_digest"],
                "INVALID_BINDING",
                "Agent event identity has conflicting content",
                422,
            )
            return False
        latest = self.c.scalar(
            sa.select(sa.func.max(t.agent_events.c.source_revision)).where(
                t.agent_events.c.source == record["source"],
                t.agent_events.c.stream_id == record["stream_id"],
            )
        )
        same_revision = one(
            self.c,
            sa.select(t.agent_events).where(
                t.agent_events.c.source == record["source"],
                t.agent_events.c.stream_id == record["stream_id"],
                t.agent_events.c.source_revision == record["source_revision"],
            ),
            required=False,
        )
        if same_revision is not None:
            require(
                same_revision["event_digest"] == record["event_digest"],
                "INVALID_BINDING",
                "Agent source revision has conflicting content",
                422,
            )
            return False
        if latest is not None and record["source_revision"] <= latest:
            return False
        self.c.execute(t.agent_events.insert().values(**record))
        return True

    def events(self, agent_run_id):
        return [
            dict(row)
            for row in self.c.execute(
                sa.select(t.agent_events)
                .where(t.agent_events.c.agent_run_id == agent_run_id)
                .order_by(t.agent_events.c.observed_at, t.agent_events.c.event_id)
            ).mappings()
        ]

    def add_outbox(self, record):
        self.c.execute(t.agent_outbox.insert().values(**record))

    def add_tool_request(self, record):
        self.c.execute(
            insert(t.agent_tool_requests)
            .values(**record)
            .on_conflict_do_nothing(
                index_elements=[
                    t.agent_tool_requests.c.proposal_id,
                    t.agent_tool_requests.c.proposal_index,
                ]
            )
        )
        saved = self.tool_request_for_proposal(record["proposal_id"], record["proposal_index"])
        require(
            saved["request_digest"] == record["request_digest"],
            "INVALID_BINDING",
            "Tool request position has conflicting content",
            422,
        )
        return saved

    def tool_request_for_proposal(self, proposal_id, proposal_index, *, lock=False):
        query = sa.select(t.agent_tool_requests).where(
            t.agent_tool_requests.c.proposal_id == proposal_id,
            t.agent_tool_requests.c.proposal_index == proposal_index,
        )
        return one(self.c, query.with_for_update() if lock else query, required=False)

    def tool_request(self, tool_request_id, *, lock=False):
        query = sa.select(t.agent_tool_requests).where(
            t.agent_tool_requests.c.tool_request_id == tool_request_id
        )
        return one(self.c, query.with_for_update() if lock else query)

    def update_tool_request(self, tool_request_id, **changes):
        if not set(changes) <= {"status", "updated_at"}:
            raise ValueError("Frozen ToolRequest fields cannot change")
        self.c.execute(
            t.agent_tool_requests.update()
            .where(t.agent_tool_requests.c.tool_request_id == tool_request_id)
            .values(**changes)
        )

    def add_tool_observation(self, record):
        self.c.execute(
            insert(t.agent_tool_observations)
            .values(**record)
            .on_conflict_do_nothing(index_elements=[t.agent_tool_observations.c.tool_request_id])
        )
        saved = self.tool_observation(record["tool_request_id"])
        require(
            saved["result_digest"] == record["result_digest"]
            and saved["status"] == record["status"],
            "INVALID_BINDING",
            "Tool observation identity has conflicting content",
            422,
        )
        return saved

    def tool_observation(self, tool_request_id):
        return one(
            self.c,
            sa.select(t.agent_tool_observations).where(
                t.agent_tool_observations.c.tool_request_id == tool_request_id
            ),
            required=False,
        )

    def tool_observations(self, agent_run_id):
        return [
            dict(row)
            for row in self.c.execute(
                sa.select(
                    t.agent_tool_observations,
                    t.agent_tool_requests.c.agent_run_id,
                    t.agent_tool_requests.c.tool_id,
                    t.agent_tool_requests.c.tool_version,
                )
                .join(
                    t.agent_tool_requests,
                    t.agent_tool_requests.c.tool_request_id
                    == t.agent_tool_observations.c.tool_request_id,
                )
                .where(t.agent_tool_requests.c.agent_run_id == agent_run_id)
                .order_by(
                    t.agent_tool_observations.c.observed_at,
                    t.agent_tool_observations.c.observation_id,
                )
            ).mappings()
        ]

    def tool_requests(self, agent_run_id):
        return [
            dict(row)
            for row in self.c.execute(
                sa.select(t.agent_tool_requests)
                .where(t.agent_tool_requests.c.agent_run_id == agent_run_id)
                .order_by(
                    t.agent_tool_requests.c.created_at, t.agent_tool_requests.c.proposal_index
                )
            ).mappings()
        ]

    def add_action_link(self, record):
        self.c.execute(
            insert(t.agent_action_links)
            .values(**record)
            .on_conflict_do_nothing(index_elements=[t.agent_action_links.c.tool_request_id])
        )
        saved = self.action_link(record["tool_request_id"])
        require(
            saved["request_digest"] == record["request_digest"]
            and saved["operation"] == record["operation"]
            and saved["business_id"] == record["business_id"],
            "INVALID_BINDING",
            "Agent action link has conflicting business identity",
            422,
        )
        return saved

    def action_link(self, tool_request_id, *, lock=False):
        query = sa.select(t.agent_action_links).where(
            t.agent_action_links.c.tool_request_id == tool_request_id
        )
        return one(self.c, query.with_for_update() if lock else query, required=False)

    def update_action_link(self, tool_request_id, **changes):
        if not set(changes) <= {"observation_revision", "last_observation_digest"}:
            raise ValueError("Frozen Agent action link fields cannot change")
        self.c.execute(
            t.agent_action_links.update()
            .where(t.agent_action_links.c.tool_request_id == tool_request_id)
            .values(**changes)
        )

    def action_links(self, agent_run_id):
        return [
            dict(row)
            for row in self.c.execute(
                sa.select(t.agent_action_links)
                .where(t.agent_action_links.c.agent_run_id == agent_run_id)
                .order_by(t.agent_action_links.c.created_at, t.agent_action_links.c.link_id)
            ).mappings()
        ]

    def runs_with_action_links(self):
        rows = self.c.execute(
            sa.select(t.agent_runs)
            .join(
                t.agent_action_links,
                t.agent_action_links.c.agent_run_id == t.agent_runs.c.agent_run_id,
            )
            .distinct()
            .order_by(t.agent_runs.c.updated_at, t.agent_runs.c.agent_run_id)
        ).mappings()
        return [dict(row) | {"links": self.action_links(row["agent_run_id"])} for row in rows]

    def add_wait_dependency(self, record):
        self.c.execute(t.agent_wait_dependencies.insert().values(**record))

    def wait_dependencies(self, agent_run_id, *, active_only=False, lock=False):
        query = sa.select(t.agent_wait_dependencies).where(
            t.agent_wait_dependencies.c.agent_run_id == agent_run_id
        )
        if active_only:
            query = query.where(t.agent_wait_dependencies.c.state.in_(["WAITING", "UNKNOWN"]))
        query = query.order_by(
            t.agent_wait_dependencies.c.created_at,
            t.agent_wait_dependencies.c.dependency_index,
        )
        if lock:
            query = query.with_for_update()
        return [dict(row) for row in self.c.execute(query).mappings()]

    def update_wait_dependency(self, dependency_id, **changes):
        allowed = {"observed_revision", "state", "state_digest", "updated_at"}
        if not set(changes) <= allowed:
            raise ValueError("Frozen Agent wait dependency fields cannot change")
        self.c.execute(
            t.agent_wait_dependencies.update()
            .where(t.agent_wait_dependencies.c.dependency_id == dependency_id)
            .values(**changes)
        )

    def cancel_wait_dependencies(self, agent_run_id, now):
        self.c.execute(
            t.agent_wait_dependencies.update()
            .where(
                t.agent_wait_dependencies.c.agent_run_id == agent_run_id,
                t.agent_wait_dependencies.c.state.in_(["WAITING", "UNKNOWN"]),
            )
            .values(state="CANCELLED", updated_at=now)
        )

    def waiting_runs(self):
        return [
            dict(row)
            for row in self.c.execute(
                sa.select(t.agent_runs)
                .where(t.agent_runs.c.status == "WAITING")
                .order_by(t.agent_runs.c.updated_at, t.agent_runs.c.agent_run_id)
            ).mappings()
        ]

    def blocked_runs(self):
        return [
            dict(row)
            for row in self.c.execute(
                sa.select(t.agent_runs)
                .where(t.agent_runs.c.status == "BLOCKED")
                .order_by(t.agent_runs.c.updated_at, t.agent_runs.c.agent_run_id)
            ).mappings()
        ]

    def update_watermark(self, record):
        self.c.execute(
            insert(t.agent_source_watermarks)
            .values(**record)
            .on_conflict_do_update(
                index_elements=[
                    t.agent_source_watermarks.c.agent_run_id,
                    t.agent_source_watermarks.c.source,
                    t.agent_source_watermarks.c.stream_id,
                ],
                set_={
                    "latest_revision": record["latest_revision"],
                    "latest_event_digest": record["latest_event_digest"],
                    "updated_at": record["updated_at"],
                },
                where=t.agent_source_watermarks.c.latest_revision < record["latest_revision"],
            )
        )

    def watermarks(self, agent_run_id):
        return [
            dict(row)
            for row in self.c.execute(
                sa.select(t.agent_source_watermarks)
                .where(t.agent_source_watermarks.c.agent_run_id == agent_run_id)
                .order_by(
                    t.agent_source_watermarks.c.source,
                    t.agent_source_watermarks.c.stream_id,
                )
            ).mappings()
        ]

    def add_runtime_recovery(self, record):
        self.c.execute(t.agent_runtime_recoveries.insert().values(**record))

    def runtime_recoveries(self, agent_run_id):
        return [
            dict(row)
            for row in self.c.execute(
                sa.select(t.agent_runtime_recoveries)
                .where(t.agent_runtime_recoveries.c.agent_run_id == agent_run_id)
                .order_by(t.agent_runtime_recoveries.c.to_epoch)
            ).mappings()
        ]

    def schedule_reconciliation(self, agent_run_id, next_attempt_at, *, category=None):
        now = self.now()
        self.c.execute(
            insert(t.agent_reconciliation_timers)
            .values(
                agent_run_id=agent_run_id,
                next_attempt_at=next_attempt_at,
                failure_count=0,
                last_category=category,
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=[t.agent_reconciliation_timers.c.agent_run_id],
                set_={
                    "next_attempt_at": next_attempt_at,
                    "last_category": category,
                    "updated_at": now,
                },
            )
        )

    def due_reconciliations(self, limit=100):
        now = self.now()
        return [
            dict(row)
            for row in self.c.execute(
                sa.select(t.agent_reconciliation_timers)
                .where(t.agent_reconciliation_timers.c.next_attempt_at <= now)
                .order_by(t.agent_reconciliation_timers.c.next_attempt_at)
                .with_for_update(skip_locked=True)
                .limit(limit)
            ).mappings()
        ]

    def clear_reconciliation(self, agent_run_id):
        self.c.execute(
            t.agent_reconciliation_timers.delete().where(
                t.agent_reconciliation_timers.c.agent_run_id == agent_run_id
            )
        )

    def add_delivery_attempt(self, record):
        self.c.execute(t.agent_delivery_attempts.insert().values(**record))

    def delivery_attempt_for_request(self, tool_request_id, *, lock=False):
        query = sa.select(t.agent_delivery_attempts).where(
            t.agent_delivery_attempts.c.tool_request_id == tool_request_id
        )
        return one(self.c, query.with_for_update() if lock else query, required=False)

    def delivery_attempts(self, agent_run_id, branch_key=None, *, lock=False):
        query = sa.select(t.agent_delivery_attempts).where(
            t.agent_delivery_attempts.c.agent_run_id == agent_run_id
        )
        if branch_key is not None:
            query = query.where(t.agent_delivery_attempts.c.branch_key == branch_key)
        query = query.order_by(
            t.agent_delivery_attempts.c.created_at,
            t.agent_delivery_attempts.c.ordinal,
        )
        if lock:
            query = query.with_for_update()
        return [dict(row) for row in self.c.execute(query).mappings()]

    def finish_delivery_attempt(self, attempt_id, **changes):
        allowed = {"outcome", "failure_category", "stop_reason", "finished_at"}
        if not set(changes) <= allowed:
            raise ValueError("Frozen Agent delivery attempt fields cannot change")
        self.c.execute(
            t.agent_delivery_attempts.update()
            .where(t.agent_delivery_attempts.c.attempt_id == attempt_id)
            .values(**changes)
        )

    def add_model_budget(self, record):
        self.c.execute(t.agent_model_budgets.insert().values(**record))

    def model_budget(self, agent_run_id, *, lock=False, required=True):
        query = sa.select(t.agent_model_budgets).where(
            t.agent_model_budgets.c.agent_run_id == agent_run_id
        )
        return one(self.c, query.with_for_update() if lock else query, required=required)

    def reserve_model_call(self, call, estimate):
        budget = self.model_budget(call["agent_run_id"], lock=True)
        checks = {
            "model calls": budget["used_calls"] + budget["reserved_calls"] + 1
            <= budget["max_model_calls"],
            "input tokens": budget["used_input_tokens"]
            + budget["reserved_input_tokens"]
            + estimate["input_tokens"]
            <= budget["max_input_tokens"],
            "output tokens": budget["used_output_tokens"]
            + budget["reserved_output_tokens"]
            + estimate["output_tokens"]
            <= budget["max_output_tokens"],
            "estimated cost": budget["used_cost_microunits"]
            + budget["reserved_cost_microunits"]
            + estimate["cost_microunits"]
            <= budget["max_estimated_cost_microunits"],
        }
        if budget["max_reasoning_tokens"] is not None:
            checks["reasoning tokens"] = (
                budget["used_reasoning_tokens"]
                + budget["reserved_reasoning_tokens"]
                + estimate["reasoning_tokens"]
                <= budget["max_reasoning_tokens"]
            )
        require(all(checks.values()), "BUDGET_BLOCKED", "Agent model budget is exhausted")
        self.c.execute(t.agent_model_calls.insert().values(**call))
        self.c.execute(
            t.agent_model_reservations.insert().values(
                model_call_id=call["model_call_id"],
                agent_run_id=call["agent_run_id"],
                reserved_input_tokens=estimate["input_tokens"],
                reserved_output_tokens=estimate["output_tokens"],
                reserved_reasoning_tokens=estimate["reasoning_tokens"],
                reserved_cost_microunits=estimate["cost_microunits"],
                actual_input_tokens=None,
                actual_output_tokens=None,
                actual_reasoning_tokens=None,
                actual_cost_microunits=None,
                call_charged=False,
                status="ACTIVE",
                created_at=call["created_at"],
                settled_at=None,
            )
        )
        self.c.execute(
            t.agent_model_budgets.update()
            .where(t.agent_model_budgets.c.agent_run_id == call["agent_run_id"])
            .values(
                reserved_calls=budget["reserved_calls"] + 1,
                reserved_input_tokens=budget["reserved_input_tokens"] + estimate["input_tokens"],
                reserved_output_tokens=budget["reserved_output_tokens"] + estimate["output_tokens"],
                reserved_reasoning_tokens=budget["reserved_reasoning_tokens"]
                + estimate["reasoning_tokens"],
                reserved_cost_microunits=budget["reserved_cost_microunits"]
                + estimate["cost_microunits"],
            )
        )
        self.c.execute(
            t.model_provider_reconciliation.insert().values(
                model_call_id=call["model_call_id"],
                next_attempt_at=call["created_at"],
                failure_count=0,
                last_error_code=None,
                owner=None,
                lease_until=None,
                generation=0,
            )
        )

    def model_call(self, model_call_id, *, lock=False, required=True):
        query = sa.select(t.agent_model_calls).where(
            t.agent_model_calls.c.model_call_id == model_call_id
        )
        return one(self.c, query.with_for_update() if lock else query, required=required)

    def model_call_for_frame(self, frame_id, attempt=None, *, lock=False):
        query = sa.select(t.agent_model_calls).where(t.agent_model_calls.c.frame_id == frame_id)
        if attempt is not None:
            query = query.where(t.agent_model_calls.c.attempt == attempt)
        else:
            query = query.order_by(t.agent_model_calls.c.attempt.desc()).limit(1)
        return one(self.c, query.with_for_update() if lock else query, required=False)

    def pending_model_frame(self, agent_run_id):
        return one(
            self.c,
            sa.select(t.agent_decision_frames)
            .join(
                t.agent_model_calls,
                t.agent_model_calls.c.frame_id == t.agent_decision_frames.c.frame_id,
            )
            .where(
                t.agent_decision_frames.c.agent_run_id == agent_run_id,
                t.agent_decision_frames.c.state == "PENDING",
            )
            .order_by(t.agent_decision_frames.c.sequence.desc())
            .limit(1),
            required=False,
        )

    def model_calls(self, agent_run_id):
        return [
            dict(row)
            for row in self.c.execute(
                sa.select(t.agent_model_calls)
                .where(t.agent_model_calls.c.agent_run_id == agent_run_id)
                .order_by(t.agent_model_calls.c.created_at, t.agent_model_calls.c.attempt)
            ).mappings()
        ]

    def update_model_call(self, model_call_id, **changes):
        allowed = {
            "status",
            "provider_ref",
            "provider_revision",
            "response",
            "response_digest",
            "proposal",
            "proposal_digest",
            "parse_errors",
            "usage_status",
            "sending_at",
            "accepted_at",
            "first_byte_at",
            "completed_at",
        }
        if not set(changes) <= allowed:
            raise ValueError("Frozen model call fields cannot change")
        self.c.execute(
            t.agent_model_calls.update()
            .where(t.agent_model_calls.c.model_call_id == model_call_id)
            .values(**changes)
        )

    def add_model_observation(self, record):
        existing = one(
            self.c,
            sa.select(t.agent_model_observations).where(
                t.agent_model_observations.c.source_event_id == record["source_event_id"]
            ),
            required=False,
        )
        if existing is not None:
            require(
                existing["observation_digest"] == record["observation_digest"],
                "INVALID_BINDING",
                "Model observation identity has conflicting content",
                422,
            )
            return False
        prior = one(
            self.c,
            sa.select(t.agent_model_observations).where(
                t.agent_model_observations.c.model_call_id == record["model_call_id"],
                t.agent_model_observations.c.provider_revision == record["provider_revision"],
                t.agent_model_observations.c.source_event_id != record["source_event_id"],
            ),
            required=False,
        )
        require(
            prior is None or prior["observation_digest"] == record["observation_digest"],
            "INVALID_BINDING",
            "Model provider revision has conflicting facts",
            422,
        )
        if prior is not None:
            return False
        self.c.execute(t.agent_model_observations.insert().values(**record))
        return True

    def update_model_reconciliation(
        self, model_call_id, *, required, next_attempt_at, error_code=None
    ):
        row = one(
            self.c,
            sa.select(t.model_provider_reconciliation)
            .where(t.model_provider_reconciliation.c.model_call_id == model_call_id)
            .with_for_update(),
        )
        if not required:
            self.c.execute(
                t.model_provider_reconciliation.delete().where(
                    t.model_provider_reconciliation.c.model_call_id == model_call_id
                )
            )
            return None
        changes = {
            "next_attempt_at": next_attempt_at,
            "failure_count": row["failure_count"] + (1 if error_code else 0),
            "last_error_code": error_code,
            "owner": None,
            "lease_until": None,
        }
        self.c.execute(
            t.model_provider_reconciliation.update()
            .where(t.model_provider_reconciliation.c.model_call_id == model_call_id)
            .values(**changes)
        )
        return row | changes

    def settle_model_reservation(self, model_call_id, *, executed, status, usage, settled_at):
        reservation = one(
            self.c,
            sa.select(t.agent_model_reservations)
            .where(t.agent_model_reservations.c.model_call_id == model_call_id)
            .with_for_update(),
        )
        if reservation["status"] in {"SETTLED", "RELEASED"}:
            return reservation
        budget = self.model_budget(reservation["agent_run_id"], lock=True)
        values = {}
        if executed is False and status in {"REJECTED", "CANCELLED"}:
            values = {
                "reserved_calls": budget["reserved_calls"] - 1,
                "reserved_input_tokens": budget["reserved_input_tokens"]
                - reservation["reserved_input_tokens"],
                "reserved_output_tokens": budget["reserved_output_tokens"]
                - reservation["reserved_output_tokens"],
                "reserved_reasoning_tokens": budget["reserved_reasoning_tokens"]
                - reservation["reserved_reasoning_tokens"],
                "reserved_cost_microunits": budget["reserved_cost_microunits"]
                - reservation["reserved_cost_microunits"],
            }
            reservation_status = "RELEASED"
            reservation_changes = {"status": reservation_status, "settled_at": settled_at}
        elif usage is not None:
            charged_now = not reservation["call_charged"]
            values = {
                "reserved_calls": budget["reserved_calls"] - (1 if charged_now else 0),
                "used_calls": budget["used_calls"] + (1 if charged_now else 0),
                "reserved_input_tokens": budget["reserved_input_tokens"]
                - reservation["reserved_input_tokens"],
                "used_input_tokens": budget["used_input_tokens"] + usage["input_tokens"],
                "reserved_output_tokens": budget["reserved_output_tokens"]
                - reservation["reserved_output_tokens"],
                "used_output_tokens": budget["used_output_tokens"] + usage["output_tokens"],
                "reserved_reasoning_tokens": budget["reserved_reasoning_tokens"]
                - reservation["reserved_reasoning_tokens"],
                "used_reasoning_tokens": budget["used_reasoning_tokens"]
                + usage["reasoning_tokens"],
                "reserved_cost_microunits": budget["reserved_cost_microunits"]
                - reservation["reserved_cost_microunits"],
                "used_cost_microunits": budget["used_cost_microunits"] + usage["cost_microunits"],
            }
            reservation_changes = {
                "actual_input_tokens": usage["input_tokens"],
                "actual_output_tokens": usage["output_tokens"],
                "actual_reasoning_tokens": usage["reasoning_tokens"],
                "actual_cost_microunits": usage["cost_microunits"],
                "call_charged": True,
                "status": "SETTLED",
                "settled_at": settled_at,
            }
        elif executed is True and not reservation["call_charged"]:
            values = {
                "reserved_calls": budget["reserved_calls"] - 1,
                "used_calls": budget["used_calls"] + 1,
            }
            reservation_changes = {"call_charged": True, "status": "UNKNOWN_USAGE"}
        else:
            return reservation
        self.c.execute(
            t.agent_model_budgets.update()
            .where(t.agent_model_budgets.c.agent_run_id == reservation["agent_run_id"])
            .values(**values)
        )
        self.c.execute(
            t.agent_model_reservations.update()
            .where(t.agent_model_reservations.c.model_call_id == model_call_id)
            .values(**reservation_changes)
        )
        return reservation | reservation_changes

    def model_usage(self, agent_run_id):
        budget = self.model_budget(agent_run_id)
        unknown_result = self.c.scalar(
            sa.select(sa.func.count())
            .select_from(t.agent_model_calls)
            .where(
                t.agent_model_calls.c.agent_run_id == agent_run_id,
                t.agent_model_calls.c.status.in_(["ACCEPTED", "RETRYABLE", "UNKNOWN"]),
            )
        )
        unknown_usage = self.c.scalar(
            sa.select(sa.func.count())
            .select_from(t.agent_model_calls)
            .where(
                t.agent_model_calls.c.agent_run_id == agent_run_id,
                t.agent_model_calls.c.usage_status == "UNKNOWN",
            )
        )
        return {key: value for key, value in budget.items() if key != "created_at"} | {
            "unknown_result_calls": unknown_result,
            "unknown_usage_calls": unknown_usage,
        }


class ServiceBindingRepository(Repository):
    def add(self, project_id, request, binding_digest):
        identity = request["service_binding_id"]
        self.c.execute(
            t.service_bindings.insert().values(
                service_binding_id=identity, project_id=project_id, current_revision=1, enabled=True
            )
        )
        self.c.execute(
            t.service_binding_revisions.insert().values(
                project_id=project_id,
                service_binding_id=identity,
                revision=1,
                protocol=request["protocol"],
                endpoint=request["endpoint"],
                credential_ref=request["credential_ref"],
                tls_ca_ref=request.get("tls_ca_ref"),
                targets=request["targets"],
                limits=request["limits"],
                binding_digest=binding_digest,
            )
        )
        return self.get(project_id, identity, 1)

    def revise(self, project_id, identity, request, binding_digest):
        head = one(
            self.c,
            sa.select(t.service_bindings)
            .where(
                t.service_bindings.c.project_id == project_id,
                t.service_bindings.c.service_binding_id == identity,
            )
            .with_for_update(),
        )
        revision = head["current_revision"] + 1
        self.c.execute(
            t.service_binding_revisions.insert().values(
                project_id=project_id,
                service_binding_id=identity,
                revision=revision,
                protocol=request["protocol"],
                endpoint=request["endpoint"],
                credential_ref=request["credential_ref"],
                tls_ca_ref=request.get("tls_ca_ref"),
                targets=request["targets"],
                limits=request["limits"],
                binding_digest=binding_digest,
            )
        )
        self.c.execute(
            t.service_bindings.update()
            .where(
                t.service_bindings.c.project_id == project_id,
                t.service_bindings.c.service_binding_id == identity,
            )
            .values(current_revision=revision)
        )
        return self.get(project_id, identity, revision)

    def set_enabled(self, project_id, identity, enabled):
        count = self.c.execute(
            t.service_bindings.update()
            .where(
                t.service_bindings.c.project_id == project_id,
                t.service_bindings.c.service_binding_id == identity,
            )
            .values(enabled=enabled)
        ).rowcount
        require(count == 1, "RESOURCE_NOT_FOUND", "Service binding not found", 404)

    def get(self, project_id, identity, revision=None):
        query = (
            sa.select(t.service_bindings, t.service_binding_revisions)
            .join(
                t.service_binding_revisions,
                t.service_binding_revisions.c.service_binding_id
                == t.service_bindings.c.service_binding_id,
            )
            .where(
                t.service_bindings.c.project_id == project_id,
                t.service_binding_revisions.c.project_id == project_id,
                t.service_bindings.c.service_binding_id == identity,
            )
        )
        if revision is None:
            query = query.where(
                t.service_binding_revisions.c.revision == t.service_bindings.c.current_revision
            )
        else:
            query = query.where(t.service_binding_revisions.c.revision == revision)
        return one(self.c, query)

    def list(self, project_id):
        return [
            dict(row)
            for row in self.c.execute(
                sa.select(t.service_bindings, t.service_binding_revisions)
                .join(
                    t.service_binding_revisions,
                    sa.and_(
                        t.service_binding_revisions.c.service_binding_id
                        == t.service_bindings.c.service_binding_id,
                        t.service_binding_revisions.c.project_id == t.service_bindings.c.project_id,
                        t.service_binding_revisions.c.revision
                        == t.service_bindings.c.current_revision,
                    ),
                )
                .where(t.service_bindings.c.project_id == project_id)
                .order_by(t.service_bindings.c.service_binding_id)
            ).mappings()
        ]
