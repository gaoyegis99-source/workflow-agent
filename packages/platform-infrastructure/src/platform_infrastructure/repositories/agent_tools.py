"""PostgreSQL repository with transaction ownership supplied by the Unit of Work."""

import sqlalchemy as sa
from platform_core.domain.control import require
from sqlalchemy.dialects.postgresql import insert

from platform_infrastructure import tables_agent as t
from platform_infrastructure.repositories.base import Repository, one


class AgentToolRepository(Repository):
    """Transaction-scoped Agent tool facts."""

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
