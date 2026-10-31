"""PostgreSQL repository with transaction ownership supplied by the Unit of Work."""

import sqlalchemy as sa
from platform_core.domain.control import require
from sqlalchemy.dialects.postgresql import insert

from platform_infrastructure import tables_agent as t
from platform_infrastructure.repositories.base import Repository, one


class AgentConversationRepository(Repository):
    """Transaction-scoped Agent conversation facts."""

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
