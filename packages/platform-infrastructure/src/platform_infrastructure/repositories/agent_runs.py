"""PostgreSQL repository with transaction ownership supplied by the Unit of Work."""

from datetime import timedelta

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from platform_infrastructure import tables_agent as t
from platform_infrastructure.repositories.base import Repository, one
from platform_infrastructure.tables_access import tasks


class AgentRunLeaseRepository(Repository):
    """Agent run identity, plan revisions, and fenced lease ownership."""

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
            .join(tasks, tasks.c.task_id == t.agent_runs.c.task_id)
            .outerjoin(t.agent_leases, t.agent_leases.c.agent_run_id == t.agent_runs.c.agent_run_id)
            .where(
                t.agent_runs.c.status.in_(["READY", "ADVANCING"]),
                tasks.c.lifecycle.in_(["OPEN", "COMPLETED"]),
                tasks.c.control == "ACTIVE",
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
