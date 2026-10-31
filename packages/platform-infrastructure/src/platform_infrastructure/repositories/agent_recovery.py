"""PostgreSQL repository with transaction ownership supplied by the Unit of Work."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from platform_infrastructure import tables_agent as t
from platform_infrastructure.repositories.base import Repository


class AgentRecoveryRepository(Repository):
    """Transaction-scoped Agent recovery facts."""

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
