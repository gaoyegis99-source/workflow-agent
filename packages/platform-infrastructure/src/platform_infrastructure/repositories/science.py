"""PostgreSQL repository with transaction ownership supplied by the Unit of Work."""

from datetime import timedelta

import sqlalchemy as sa
from platform_core.domain.control import require
from sqlalchemy.dialects.postgresql import insert

from platform_infrastructure import tables_acceptance as t
from platform_infrastructure import tables_access as access
from platform_infrastructure.repositories.base import Repository, digest_record, one


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

    def pending_checks(self, acceptance_id=None):
        query = (
            sa.select(t.acceptances)
            .join(access.actions, access.actions.c.action_id == t.acceptances.c.action_id)
            .join(access.tasks, access.tasks.c.task_id == t.acceptances.c.task_id)
            .where(
                ~sa.exists(
                    sa.select(t.check_envelopes.c.acceptance_id).where(
                        t.check_envelopes.c.acceptance_id == t.acceptances.c.acceptance_id
                    )
                ),
                access.actions.c.stopped.is_(True),
                sa.or_(
                    t.acceptances.c.status == "QUEUED",
                    sa.and_(
                        t.acceptances.c.status == "PASS",
                        access.tasks.c.lifecycle == "OPEN",
                        access.tasks.c.control == "ACTIVE",
                        access.tasks.c.spec_revision == t.acceptances.c.spec_revision,
                    ),
                ),
            )
        )
        if acceptance_id is not None:
            query = query.where(t.acceptances.c.acceptance_id == acceptance_id)
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

    @staticmethod
    def _eligible_status():
        return sa.or_(
            t.acceptances.c.status == "QUEUED",
            sa.and_(
                t.acceptances.c.status == "PASS",
                access.tasks.c.lifecycle == "OPEN",
                access.tasks.c.control == "ACTIVE",
                access.tasks.c.spec_revision == t.acceptances.c.spec_revision,
            ),
        )

    @classmethod
    def _r2_eligible(cls):
        return sa.or_(
            cls._eligible_status(),
            sa.and_(
                t.acceptances.c.status == "STALE",
                t.check_envelopes.c.released.is_(False),
            ),
        )

    def active_r2_checks(self, acceptance_id=None):
        query = (
            sa.select(t.acceptances)
            .join(
                t.check_envelopes,
                t.check_envelopes.c.acceptance_id == t.acceptances.c.acceptance_id,
            )
            .join(access.tasks, access.tasks.c.task_id == t.acceptances.c.task_id)
            .where(self._r2_eligible())
        )
        if acceptance_id is not None:
            query = query.where(t.acceptances.c.acceptance_id == acceptance_id)
        return [
            dict(row)
            for row in self.c.execute(
                query.order_by(t.acceptances.c.acceptance_id).limit(100)
            ).mappings()
        ]

    @staticmethod
    def _acceptance_lease(acceptance_id, owner, generation):
        return sa.and_(
            t.acceptances.c.acceptance_id == acceptance_id,
            t.acceptances.c.owner == owner,
            t.acceptances.c.generation == generation,
            t.acceptances.c.lease_until > sa.func.clock_timestamp(),
        )

    def claim_acceptance(self, owner, lease_seconds):
        """Claim one ready acceptance in deterministic order.

        R2 acceptances are ready whenever their coverage envelope needs one
        transition; ordinary acceptances only become ready after the checker
        action stopped.  A claim is intentionally independent of action
        leases, so one worker cannot accidentally own another lane's effect.
        """

        now = self.now()
        query = (
            sa.select(t.acceptances)
            .join(access.tasks, access.tasks.c.task_id == t.acceptances.c.task_id)
            .outerjoin(
                t.check_envelopes,
                t.check_envelopes.c.acceptance_id == t.acceptances.c.acceptance_id,
            )
            .outerjoin(access.actions, access.actions.c.action_id == t.acceptances.c.action_id)
            .where(
                sa.or_(
                    t.acceptances.c.lease_until.is_(None),
                    t.acceptances.c.lease_until <= now,
                ),
                sa.or_(
                    sa.and_(t.check_envelopes.c.acceptance_id.is_not(None), self._r2_eligible()),
                    sa.and_(
                        t.check_envelopes.c.acceptance_id.is_(None),
                        access.actions.c.stopped.is_(True),
                        self._eligible_status(),
                    ),
                ),
            )
            .order_by(t.acceptances.c.acceptance_id)
            # PostgreSQL cannot lock the nullable side of an outer join.  The
            # acceptance row is the only ownership boundary for this lane;
            # the joined rows are eligibility inputs and must remain unlocked.
            .with_for_update(of=t.acceptances, skip_locked=True)
            .limit(1)
        )
        row = one(self.c, query, required=False)
        if row is None:
            return None
        changes = {
            "owner": owner,
            "generation": row["generation"] + 1,
            "lease_until": now + timedelta(seconds=lease_seconds),
            "attempts": row["attempts"] + 1,
        }
        self.c.execute(
            t.acceptances.update()
            .where(t.acceptances.c.acceptance_id == row["acceptance_id"])
            .values(**changes)
        )
        return row | {"owner": owner, "generation": row["generation"] + 1}

    def acceptance_owned(self, acceptance_id, owner, generation):
        row = one(
            self.c,
            sa.select(t.acceptances)
            .where(t.acceptances.c.acceptance_id == acceptance_id)
            .with_for_update(),
            required=False,
        )
        return bool(
            row
            and row["owner"] == owner
            and row["generation"] == generation
            and row["lease_until"] is not None
            and row["lease_until"] > self.now()
        )

    def renew_acceptance(self, acceptance_id, owner, generation, lease_seconds):
        return (
            self.c.execute(
                t.acceptances.update()
                .where(self._acceptance_lease(acceptance_id, owner, generation))
                .values(lease_until=self.now() + timedelta(seconds=lease_seconds))
            ).rowcount
            == 1
        )

    def release_acceptance(self, acceptance_id, owner, generation):
        return (
            self.c.execute(
                t.acceptances.update()
                .where(self._acceptance_lease(acceptance_id, owner, generation))
                .values(owner=None, lease_until=None)
            ).rowcount
            == 1
        )

    def finish_acceptance(self, acceptance_id, owner, generation):
        """Finish one orchestration turn while retaining the acceptance state.

        ``acceptances`` is a state machine rather than an outbox row, so a
        completed turn is made eligible for the next transition by releasing
        its worker lease.  The same live-lease predicate prevents a stale
        generation from releasing a newer owner's work.
        """

        return self.release_acceptance(acceptance_id, owner, generation)

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
