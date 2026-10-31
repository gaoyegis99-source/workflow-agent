"""PostgreSQL repository with transaction ownership supplied by the Unit of Work."""

import sqlalchemy as sa
from platform_core.domain.control import require

from platform_infrastructure import tables_agent as t
from platform_infrastructure.repositories.base import Repository, one


class AgentBudgetRepository(Repository):
    """Transaction-scoped Agent budget facts."""

    def add_model_budget(self, record):
        self.c.execute(t.agent_model_budgets.insert().values(**record))

    def model_budget(self, agent_run_id, *, lock=False, required=True):
        query = sa.select(t.agent_model_budgets).where(
            t.agent_model_budgets.c.agent_run_id == agent_run_id
        )
        return one(self.c, query.with_for_update() if lock else query, required=required)

    def reserve_model_call(self, call, estimate):
        budget = self.model_budget(call["agent_run_id"], lock=True)
        checks = {
            "model calls": budget["used_calls"] + budget["reserved_calls"] + 1
            <= budget["max_model_calls"],
            "input tokens": budget["used_input_tokens"]
            + budget["reserved_input_tokens"]
            + estimate["input_tokens"]
            <= budget["max_input_tokens"],
            "output tokens": budget["used_output_tokens"]
            + budget["reserved_output_tokens"]
            + estimate["output_tokens"]
            <= budget["max_output_tokens"],
            "estimated cost": budget["used_cost_microunits"]
            + budget["reserved_cost_microunits"]
            + estimate["cost_microunits"]
            <= budget["max_estimated_cost_microunits"],
        }
        if budget["max_reasoning_tokens"] is not None:
            checks["reasoning tokens"] = (
                budget["used_reasoning_tokens"]
                + budget["reserved_reasoning_tokens"]
                + estimate["reasoning_tokens"]
                <= budget["max_reasoning_tokens"]
            )
        require(all(checks.values()), "BUDGET_BLOCKED", "Agent model budget is exhausted")
        self.c.execute(t.agent_model_calls.insert().values(**call))
        self.c.execute(
            t.agent_model_reservations.insert().values(
                model_call_id=call["model_call_id"],
                agent_run_id=call["agent_run_id"],
                reserved_input_tokens=estimate["input_tokens"],
                reserved_output_tokens=estimate["output_tokens"],
                reserved_reasoning_tokens=estimate["reasoning_tokens"],
                reserved_cost_microunits=estimate["cost_microunits"],
                actual_input_tokens=None,
                actual_output_tokens=None,
                actual_reasoning_tokens=None,
                actual_cost_microunits=None,
                call_charged=False,
                status="ACTIVE",
                created_at=call["created_at"],
                settled_at=None,
            )
        )
        self.c.execute(
            t.agent_model_budgets.update()
            .where(t.agent_model_budgets.c.agent_run_id == call["agent_run_id"])
            .values(
                reserved_calls=budget["reserved_calls"] + 1,
                reserved_input_tokens=budget["reserved_input_tokens"] + estimate["input_tokens"],
                reserved_output_tokens=budget["reserved_output_tokens"] + estimate["output_tokens"],
                reserved_reasoning_tokens=budget["reserved_reasoning_tokens"]
                + estimate["reasoning_tokens"],
                reserved_cost_microunits=budget["reserved_cost_microunits"]
                + estimate["cost_microunits"],
            )
        )
        self.c.execute(
            t.model_provider_reconciliation.insert().values(
                model_call_id=call["model_call_id"],
                next_attempt_at=call["created_at"],
                failure_count=0,
                last_error_code=None,
                owner=None,
                lease_until=None,
                generation=0,
            )
        )

    def model_call(self, model_call_id, *, lock=False, required=True):
        query = sa.select(t.agent_model_calls).where(
            t.agent_model_calls.c.model_call_id == model_call_id
        )
        return one(self.c, query.with_for_update() if lock else query, required=required)

    def model_call_for_frame(self, frame_id, attempt=None, *, lock=False):
        query = sa.select(t.agent_model_calls).where(t.agent_model_calls.c.frame_id == frame_id)
        if attempt is not None:
            query = query.where(t.agent_model_calls.c.attempt == attempt)
        else:
            query = query.order_by(t.agent_model_calls.c.attempt.desc()).limit(1)
        return one(self.c, query.with_for_update() if lock else query, required=False)

    def pending_model_frame(self, agent_run_id):
        return one(
            self.c,
            sa.select(t.agent_decision_frames)
            .join(
                t.agent_model_calls,
                t.agent_model_calls.c.frame_id == t.agent_decision_frames.c.frame_id,
            )
            .where(
                t.agent_decision_frames.c.agent_run_id == agent_run_id,
                t.agent_decision_frames.c.state == "PENDING",
            )
            .order_by(t.agent_decision_frames.c.sequence.desc())
            .limit(1),
            required=False,
        )

    def model_calls(self, agent_run_id):
        return [
            dict(row)
            for row in self.c.execute(
                sa.select(t.agent_model_calls)
                .where(t.agent_model_calls.c.agent_run_id == agent_run_id)
                .order_by(t.agent_model_calls.c.created_at, t.agent_model_calls.c.attempt)
            ).mappings()
        ]

    def update_model_call(self, model_call_id, **changes):
        allowed = {
            "status",
            "provider_ref",
            "provider_revision",
            "response",
            "response_digest",
            "proposal",
            "proposal_digest",
            "parse_errors",
            "usage_status",
            "sending_at",
            "accepted_at",
            "first_byte_at",
            "completed_at",
        }
        if not set(changes) <= allowed:
            raise ValueError("Frozen model call fields cannot change")
        self.c.execute(
            t.agent_model_calls.update()
            .where(t.agent_model_calls.c.model_call_id == model_call_id)
            .values(**changes)
        )

    def add_model_observation(self, record):
        existing = one(
            self.c,
            sa.select(t.agent_model_observations).where(
                t.agent_model_observations.c.source_event_id == record["source_event_id"]
            ),
            required=False,
        )
        if existing is not None:
            require(
                existing["observation_digest"] == record["observation_digest"],
                "INVALID_BINDING",
                "Model observation identity has conflicting content",
                422,
            )
            return False
        prior = one(
            self.c,
            sa.select(t.agent_model_observations).where(
                t.agent_model_observations.c.model_call_id == record["model_call_id"],
                t.agent_model_observations.c.provider_revision == record["provider_revision"],
                t.agent_model_observations.c.source_event_id != record["source_event_id"],
            ),
            required=False,
        )
        require(
            prior is None or prior["observation_digest"] == record["observation_digest"],
            "INVALID_BINDING",
            "Model provider revision has conflicting facts",
            422,
        )
        if prior is not None:
            return False
        self.c.execute(t.agent_model_observations.insert().values(**record))
        return True

    def update_model_reconciliation(
        self, model_call_id, *, required, next_attempt_at, error_code=None
    ):
        row = one(
            self.c,
            sa.select(t.model_provider_reconciliation)
            .where(t.model_provider_reconciliation.c.model_call_id == model_call_id)
            .with_for_update(),
        )
        if not required:
            self.c.execute(
                t.model_provider_reconciliation.delete().where(
                    t.model_provider_reconciliation.c.model_call_id == model_call_id
                )
            )
            return None
        changes = {
            "next_attempt_at": next_attempt_at,
            "failure_count": row["failure_count"] + (1 if error_code else 0),
            "last_error_code": error_code,
            "owner": None,
            "lease_until": None,
        }
        self.c.execute(
            t.model_provider_reconciliation.update()
            .where(t.model_provider_reconciliation.c.model_call_id == model_call_id)
            .values(**changes)
        )
        return row | changes

    def settle_model_reservation(self, model_call_id, *, executed, status, usage, settled_at):
        reservation = one(
            self.c,
            sa.select(t.agent_model_reservations)
            .where(t.agent_model_reservations.c.model_call_id == model_call_id)
            .with_for_update(),
        )
        if reservation["status"] in {"SETTLED", "RELEASED"}:
            return reservation
        budget = self.model_budget(reservation["agent_run_id"], lock=True)
        values = {}
        if executed is False and status in {"REJECTED", "CANCELLED"}:
            values = {
                "reserved_calls": budget["reserved_calls"] - 1,
                "reserved_input_tokens": budget["reserved_input_tokens"]
                - reservation["reserved_input_tokens"],
                "reserved_output_tokens": budget["reserved_output_tokens"]
                - reservation["reserved_output_tokens"],
                "reserved_reasoning_tokens": budget["reserved_reasoning_tokens"]
                - reservation["reserved_reasoning_tokens"],
                "reserved_cost_microunits": budget["reserved_cost_microunits"]
                - reservation["reserved_cost_microunits"],
            }
            reservation_status = "RELEASED"
            reservation_changes = {"status": reservation_status, "settled_at": settled_at}
        elif usage is not None:
            charged_now = not reservation["call_charged"]
            values = {
                "reserved_calls": budget["reserved_calls"] - (1 if charged_now else 0),
                "used_calls": budget["used_calls"] + (1 if charged_now else 0),
                "reserved_input_tokens": budget["reserved_input_tokens"]
                - reservation["reserved_input_tokens"],
                "used_input_tokens": budget["used_input_tokens"] + usage["input_tokens"],
                "reserved_output_tokens": budget["reserved_output_tokens"]
                - reservation["reserved_output_tokens"],
                "used_output_tokens": budget["used_output_tokens"] + usage["output_tokens"],
                "reserved_reasoning_tokens": budget["reserved_reasoning_tokens"]
                - reservation["reserved_reasoning_tokens"],
                "used_reasoning_tokens": budget["used_reasoning_tokens"]
                + usage["reasoning_tokens"],
                "reserved_cost_microunits": budget["reserved_cost_microunits"]
                - reservation["reserved_cost_microunits"],
                "used_cost_microunits": budget["used_cost_microunits"] + usage["cost_microunits"],
            }
            reservation_changes = {
                "actual_input_tokens": usage["input_tokens"],
                "actual_output_tokens": usage["output_tokens"],
                "actual_reasoning_tokens": usage["reasoning_tokens"],
                "actual_cost_microunits": usage["cost_microunits"],
                "call_charged": True,
                "status": "SETTLED",
                "settled_at": settled_at,
            }
        elif executed is True and not reservation["call_charged"]:
            values = {
                "reserved_calls": budget["reserved_calls"] - 1,
                "used_calls": budget["used_calls"] + 1,
            }
            reservation_changes = {"call_charged": True, "status": "UNKNOWN_USAGE"}
        else:
            return reservation
        self.c.execute(
            t.agent_model_budgets.update()
            .where(t.agent_model_budgets.c.agent_run_id == reservation["agent_run_id"])
            .values(**values)
        )
        self.c.execute(
            t.agent_model_reservations.update()
            .where(t.agent_model_reservations.c.model_call_id == model_call_id)
            .values(**reservation_changes)
        )
        return reservation | reservation_changes

    def model_usage(self, agent_run_id):
        budget = self.model_budget(agent_run_id)
        unknown_result = self.c.scalar(
            sa.select(sa.func.count())
            .select_from(t.agent_model_calls)
            .where(
                t.agent_model_calls.c.agent_run_id == agent_run_id,
                t.agent_model_calls.c.status.in_(["ACCEPTED", "RETRYABLE", "UNKNOWN"]),
            )
        )
        unknown_usage = self.c.scalar(
            sa.select(sa.func.count())
            .select_from(t.agent_model_calls)
            .where(
                t.agent_model_calls.c.agent_run_id == agent_run_id,
                t.agent_model_calls.c.usage_status == "UNKNOWN",
            )
        )
        return {key: value for key, value in budget.items() if key != "created_at"} | {
            "unknown_result_calls": unknown_result,
            "unknown_usage_calls": unknown_usage,
        }
