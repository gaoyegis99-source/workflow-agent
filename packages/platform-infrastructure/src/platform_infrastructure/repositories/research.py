"""PostgreSQL repository with transaction ownership supplied by the Unit of Work."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from platform_infrastructure import tables_research as t
from platform_infrastructure.repositories.base import Repository, one


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
