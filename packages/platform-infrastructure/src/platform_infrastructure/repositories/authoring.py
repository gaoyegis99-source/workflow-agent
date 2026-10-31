"""PostgreSQL repository with transaction ownership supplied by the Unit of Work."""

from datetime import timedelta

import sqlalchemy as sa
from platform_core.domain.control import require
from sqlalchemy.dialects.postgresql import insert

from platform_infrastructure import tables_workflows as t
from platform_infrastructure.repositories.base import Repository, one


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
