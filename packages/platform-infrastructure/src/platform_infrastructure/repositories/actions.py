"""PostgreSQL repository with transaction ownership supplied by the Unit of Work."""

import sqlalchemy as sa

from platform_infrastructure import tables_access as t
from platform_infrastructure.repositories.base import Repository, one


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
