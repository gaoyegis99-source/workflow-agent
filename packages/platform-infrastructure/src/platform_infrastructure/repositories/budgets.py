"""PostgreSQL repository with transaction ownership supplied by the Unit of Work."""

from dataclasses import asdict

import sqlalchemy as sa
from platform_core.domain.control import Budget

from platform_infrastructure import tables_access as t
from platform_infrastructure.repositories.base import Repository, one


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
