"""PostgreSQL repository with transaction ownership supplied by the Unit of Work."""

from dataclasses import asdict

import sqlalchemy as sa
from platform_core.domain.control import Task

from platform_infrastructure import tables_access as t
from platform_infrastructure.repositories.base import Repository, one


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
