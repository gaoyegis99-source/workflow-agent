"""Platform Agent runtime ledger table declarations."""

from .tables_shared import ID, JSONB, TIME, sa, table

# P6.1 Platform-owned Agent ledger. Framework checkpoints are kept in a
# different database; these records remain sufficient to fence and rebuild a
# runtime epoch without treating framework state as a business fact.
agent_runs = table(
    "agent_runs",
    sa.Column("agent_run_id", ID, primary_key=True),
    sa.Column("project_id", ID, sa.ForeignKey("projects.project_id"), nullable=False),
    sa.Column("task_id", ID, sa.ForeignKey("tasks.task_id"), nullable=False, unique=True),
    sa.Column("status", sa.String(24), nullable=False),
    sa.Column("epoch", sa.Integer, nullable=False),
    sa.Column("generation", sa.BigInteger, nullable=False),
    sa.Column("runtime_ref", ID, nullable=False, unique=True),
    sa.Column("runtime_profile", JSONB, nullable=False),
    sa.Column("runtime_profile_digest", sa.String(71), nullable=False),
    sa.Column("task_spec_revision", sa.Integer, nullable=False),
    sa.Column("control_version", sa.Integer, nullable=False),
    sa.Column("plan_revision", sa.Integer, nullable=False, server_default="0"),
    sa.Column("frame_sequence", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("accepted_proposals", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("wake_revision", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("blocked_reason", sa.String(64)),
    sa.Column("created_by", ID, sa.ForeignKey("principals.principal_id"), nullable=False),
    sa.Column("created_at", TIME, nullable=False),
    sa.Column("updated_at", TIME, nullable=False),
    sa.CheckConstraint(
        "status IN ('READY','ADVANCING','WAITING','QUESTION','BLOCKED','STOPPED')",
        name="ck_agent_run_status",
    ),
    sa.CheckConstraint(
        "epoch > 0 AND generation > 0 AND task_spec_revision > 0 "
        "AND control_version > 0 AND plan_revision >= 0 AND frame_sequence >= 0 "
        "AND accepted_proposals >= 0 AND wake_revision >= 0",
        name="ck_agent_run_revisions",
    ),
)
agent_plan_revisions = table(
    "agent_plan_revisions",
    sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), primary_key=True),
    sa.Column("revision", sa.Integer, primary_key=True),
    sa.Column("task_spec_revision", sa.Integer, nullable=False),
    sa.Column("frame_id", ID, nullable=False),
    sa.Column("generation", sa.BigInteger, nullable=False),
    sa.Column("plan", JSONB, nullable=False),
    sa.Column("plan_digest", sa.String(71), nullable=False),
    sa.Column("created_at", TIME, nullable=False),
    sa.CheckConstraint("revision > 0", name="ck_agent_plan_revision"),
)
agent_context_manifests = table(
    "agent_context_manifests",
    sa.Column("manifest_id", ID, primary_key=True),
    sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), nullable=False),
    sa.Column("task_spec_revision", sa.Integer, nullable=False),
    sa.Column("manifest", JSONB, nullable=False),
    sa.Column("manifest_digest", sa.String(71), nullable=False),
    sa.Column("created_at", TIME, nullable=False),
    sa.UniqueConstraint("agent_run_id", "manifest_digest", name="uq_agent_context_manifest_digest"),
)
agent_decision_frames = table(
    "agent_decision_frames",
    sa.Column("frame_id", ID, primary_key=True),
    sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), nullable=False),
    sa.Column("sequence", sa.BigInteger, nullable=False),
    sa.Column("epoch", sa.Integer, nullable=False),
    sa.Column("generation", sa.BigInteger, nullable=False),
    sa.Column("task_spec_revision", sa.Integer, nullable=False),
    sa.Column("control_version", sa.Integer, nullable=False),
    sa.Column(
        "context_manifest_id",
        ID,
        sa.ForeignKey("agent_context_manifests.manifest_id"),
        nullable=False,
    ),
    sa.Column("context_manifest_digest", sa.String(71), nullable=False),
    sa.Column("payload", JSONB, nullable=False),
    sa.Column("frame_digest", sa.String(71), nullable=False),
    sa.Column("state", sa.String(16), nullable=False),
    sa.Column("created_at", TIME, nullable=False),
    sa.UniqueConstraint("agent_run_id", "sequence", name="uq_agent_frame_sequence"),
    sa.CheckConstraint("state IN ('PENDING','ACCEPTED','STALE')", name="ck_agent_frame_state"),
)
agent_proposals = table(
    "agent_proposals",
    sa.Column("proposal_id", ID, primary_key=True),
    sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), nullable=False),
    sa.Column(
        "frame_id", ID, sa.ForeignKey("agent_decision_frames.frame_id"), nullable=False, unique=True
    ),
    sa.Column("epoch", sa.Integer, nullable=False),
    sa.Column("generation", sa.BigInteger, nullable=False),
    sa.Column("kind", sa.String(32), nullable=False),
    sa.Column("proposal", JSONB, nullable=False),
    sa.Column("proposal_digest", sa.String(71), nullable=False),
    sa.Column("accepted_at", TIME, nullable=False),
)
agent_questions = table(
    "agent_questions",
    sa.Column("question_id", ID, primary_key=True),
    sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), nullable=False),
    sa.Column("frame_id", ID, sa.ForeignKey("agent_decision_frames.frame_id"), nullable=False),
    sa.Column("gap_id", ID, nullable=False),
    sa.Column("question", JSONB, nullable=False),
    sa.Column("status", sa.String(16), nullable=False),
    sa.Column("created_at", TIME, nullable=False),
    sa.UniqueConstraint("agent_run_id", "gap_id", name="uq_agent_question_gap"),
    sa.CheckConstraint(
        "status IN ('OPEN','ANSWERED','CANCELLED')", name="ck_agent_question_status"
    ),
)
agent_answers = table(
    "agent_answers",
    sa.Column("answer_id", ID, primary_key=True),
    sa.Column(
        "question_id", ID, sa.ForeignKey("agent_questions.question_id"), nullable=False, unique=True
    ),
    sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), nullable=False),
    sa.Column("task_spec_revision", sa.Integer, nullable=False),
    sa.Column("answer", JSONB, nullable=False),
    sa.Column("answer_digest", sa.String(71), nullable=False),
    sa.Column("answered_by", ID, sa.ForeignKey("principals.principal_id"), nullable=False),
    sa.Column("answered_at", TIME, nullable=False),
)
agent_events = table(
    "agent_events",
    sa.Column("event_id", ID, primary_key=True),
    sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), nullable=False),
    sa.Column("source", sa.String(32), nullable=False),
    sa.Column("stream_id", ID, nullable=False),
    sa.Column("source_revision", sa.BigInteger, nullable=False),
    sa.Column("kind", sa.String(64), nullable=False),
    sa.Column("payload", JSONB, nullable=False),
    sa.Column("event_digest", sa.String(71), nullable=False),
    sa.Column("observed_at", TIME, nullable=False),
    sa.UniqueConstraint("source", "stream_id", "source_revision", name="uq_agent_event_revision"),
)
agent_inbox = table(
    "agent_inbox",
    sa.Column("source", sa.String(32), primary_key=True),
    sa.Column("event_id", ID, primary_key=True),
    sa.Column("event_digest", sa.String(71), nullable=False),
    sa.Column("observed_at", TIME, nullable=False),
)
agent_wakeups = table(
    "agent_wakeups",
    sa.Column("wake_id", ID, primary_key=True),
    sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), nullable=False),
    sa.Column("revision", sa.BigInteger, nullable=False),
    sa.Column("reason", sa.String(32), nullable=False),
    sa.Column("event_ids", JSONB, nullable=False),
    sa.Column("state", sa.String(16), nullable=False),
    sa.Column("created_at", TIME, nullable=False),
    sa.Column("consumed_at", TIME),
    sa.UniqueConstraint("agent_run_id", "revision", name="uq_agent_wakeup_revision"),
    sa.CheckConstraint("state IN ('PENDING','CONSUMED','CANCELLED')", name="ck_agent_wakeup_state"),
)
agent_outbox = table(
    "agent_outbox",
    sa.Column("event_id", ID, primary_key=True),
    sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), nullable=False),
    sa.Column("kind", sa.String(32), nullable=False),
    sa.Column("payload", JSONB, nullable=False),
    sa.Column("done", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("available_at", TIME, nullable=False),
    sa.Column("owner", ID),
    sa.Column("lease_until", TIME),
    sa.Column("generation", sa.BigInteger, nullable=False, server_default="0"),
)
agent_leases = table(
    "agent_leases",
    sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), primary_key=True),
    sa.Column("owner", ID, nullable=False),
    sa.Column("generation", sa.BigInteger, nullable=False),
    sa.Column("lease_until", TIME, nullable=False),
    sa.Column("acquired_at", TIME, nullable=False),
    sa.CheckConstraint("generation > 0", name="ck_agent_lease_generation"),
)
sa.Index("ix_agent_runs_ready", agent_runs.c.status, agent_runs.c.updated_at)
sa.Index(
    "ix_agent_frames_run", agent_decision_frames.c.agent_run_id, agent_decision_frames.c.sequence
)
sa.Index("ix_agent_events_run", agent_events.c.agent_run_id, agent_events.c.observed_at)
sa.Index(
    "ix_agent_wakeups_pending",
    agent_wakeups.c.agent_run_id,
    agent_wakeups.c.revision,
    postgresql_where=agent_wakeups.c.state == "PENDING",
)
sa.Index(
    "ix_agent_outbox_ready",
    agent_outbox.c.available_at,
    postgresql_where=agent_outbox.c.done.is_(False),
)

# P6.2 model calls are accounted separately from scientific execution.  A
# reservation is committed before provider I/O and remains held while either
# the result or its usage is unknown.
agent_model_budgets = table(
    "agent_model_budgets",
    sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), primary_key=True),
    sa.Column("task_id", ID, sa.ForeignKey("tasks.task_id"), nullable=False, unique=True),
    sa.Column("profile_id", ID, nullable=False),
    sa.Column("profile_digest", sa.String(71), nullable=False),
    sa.Column("max_model_calls", sa.Integer, nullable=False),
    sa.Column("max_input_tokens", sa.BigInteger, nullable=False),
    sa.Column("max_output_tokens", sa.BigInteger, nullable=False),
    sa.Column("max_reasoning_tokens", sa.BigInteger),
    sa.Column("max_estimated_cost_microunits", sa.BigInteger, nullable=False),
    sa.Column("max_agent_wall_seconds", sa.Integer, nullable=False),
    sa.Column("max_decision_frames", sa.Integer, nullable=False),
    sa.Column("used_calls", sa.Integer, nullable=False, server_default="0"),
    sa.Column("reserved_calls", sa.Integer, nullable=False, server_default="0"),
    sa.Column("used_input_tokens", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("reserved_input_tokens", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("used_output_tokens", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("reserved_output_tokens", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("used_reasoning_tokens", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("reserved_reasoning_tokens", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("used_cost_microunits", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("reserved_cost_microunits", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("created_at", TIME, nullable=False),
)
agent_model_calls = table(
    "agent_model_calls",
    sa.Column("model_call_id", ID, primary_key=True),
    sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), nullable=False),
    sa.Column("frame_id", ID, sa.ForeignKey("agent_decision_frames.frame_id"), nullable=False),
    sa.Column("attempt", sa.Integer, nullable=False),
    sa.Column("repair_of_model_call_id", ID, sa.ForeignKey("agent_model_calls.model_call_id")),
    sa.Column("epoch", sa.Integer, nullable=False),
    sa.Column("generation", sa.BigInteger, nullable=False),
    sa.Column("profile_id", ID, nullable=False),
    sa.Column("profile_digest", sa.String(71), nullable=False),
    sa.Column("context_digest", sa.String(71), nullable=False),
    sa.Column("tool_catalog_digest", sa.String(71), nullable=False),
    sa.Column("request", JSONB, nullable=False),
    sa.Column("request_digest", sa.String(71), nullable=False),
    sa.Column("status", sa.String(24), nullable=False),
    sa.Column("provider_ref", ID),
    sa.Column("provider_revision", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("response", JSONB),
    sa.Column("response_digest", sa.String(71)),
    sa.Column("proposal", JSONB),
    sa.Column("proposal_digest", sa.String(71)),
    sa.Column("parse_errors", JSONB),
    sa.Column("usage_status", sa.String(24), nullable=False),
    sa.Column("contains_sensitive_context", sa.Boolean, nullable=False),
    sa.Column("created_at", TIME, nullable=False),
    sa.Column("sending_at", TIME),
    sa.Column("accepted_at", TIME),
    sa.Column("first_byte_at", TIME),
    sa.Column("completed_at", TIME),
    sa.UniqueConstraint("frame_id", "attempt", name="uq_agent_model_call_attempt"),
)
agent_model_reservations = table(
    "agent_model_reservations",
    sa.Column(
        "model_call_id", ID, sa.ForeignKey("agent_model_calls.model_call_id"), primary_key=True
    ),
    sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), nullable=False),
    sa.Column("reserved_input_tokens", sa.BigInteger, nullable=False),
    sa.Column("reserved_output_tokens", sa.BigInteger, nullable=False),
    sa.Column("reserved_reasoning_tokens", sa.BigInteger, nullable=False),
    sa.Column("reserved_cost_microunits", sa.BigInteger, nullable=False),
    sa.Column("actual_input_tokens", sa.BigInteger),
    sa.Column("actual_output_tokens", sa.BigInteger),
    sa.Column("actual_reasoning_tokens", sa.BigInteger),
    sa.Column("actual_cost_microunits", sa.BigInteger),
    sa.Column("call_charged", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("status", sa.String(24), nullable=False),
    sa.Column("created_at", TIME, nullable=False),
    sa.Column("settled_at", TIME),
)
agent_model_observations = table(
    "agent_model_observations",
    sa.Column("source_event_id", ID, primary_key=True),
    sa.Column(
        "model_call_id", ID, sa.ForeignKey("agent_model_calls.model_call_id"), nullable=False
    ),
    sa.Column("provider_revision", sa.BigInteger, nullable=False),
    sa.Column("provider_ref", ID),
    sa.Column("status", sa.String(24), nullable=False),
    sa.Column("executed", sa.Boolean),
    sa.Column("response_digest", sa.String(71)),
    sa.Column("usage", JSONB),
    sa.Column("observation_digest", sa.String(71), nullable=False),
    sa.Column("observed_at", TIME, nullable=False),
    sa.UniqueConstraint(
        "model_call_id", "provider_revision", name="uq_agent_model_observation_revision"
    ),
)
model_provider_reconciliation = table(
    "model_provider_reconciliation",
    sa.Column(
        "model_call_id", ID, sa.ForeignKey("agent_model_calls.model_call_id"), primary_key=True
    ),
    sa.Column("next_attempt_at", TIME, nullable=False),
    sa.Column("failure_count", sa.Integer, nullable=False, server_default="0"),
    sa.Column("last_error_code", sa.String(64)),
    sa.Column("owner", ID),
    sa.Column("lease_until", TIME),
    sa.Column("generation", sa.BigInteger, nullable=False, server_default="0"),
)
sa.Index(
    "ix_agent_model_calls_run", agent_model_calls.c.agent_run_id, agent_model_calls.c.created_at
)
sa.Index("ix_model_reconciliation_ready", model_provider_reconciliation.c.next_attempt_at)

# P6.3 read tools are separated from free-form Agent events.  A request is
# committed before service I/O; the single observation is immutable and is the
# only content admitted to a later model frame.
agent_tool_requests = table(
    "agent_tool_requests",
    sa.Column("tool_request_id", ID, primary_key=True),
    sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), nullable=False),
    sa.Column("proposal_id", ID, sa.ForeignKey("agent_proposals.proposal_id"), nullable=False),
    sa.Column("frame_id", ID, sa.ForeignKey("agent_decision_frames.frame_id"), nullable=False),
    sa.Column("proposal_index", sa.Integer, nullable=False),
    sa.Column("model_call_id", ID, sa.ForeignKey("agent_model_calls.model_call_id")),
    sa.Column("call_id", ID, nullable=False),
    sa.Column("tool_id", sa.String(128), nullable=False),
    sa.Column("tool_version", sa.String(128), nullable=False),
    sa.Column("purpose", sa.String(1000), nullable=False),
    sa.Column("arguments", JSONB, nullable=False),
    sa.Column("request_digest", sa.String(71), nullable=False),
    sa.Column("status", sa.String(24), nullable=False),
    sa.Column("created_at", TIME, nullable=False),
    sa.Column("updated_at", TIME, nullable=False),
    sa.UniqueConstraint("proposal_id", "proposal_index", name="uq_agent_tool_proposal_index"),
    sa.CheckConstraint("proposal_index >= 0", name="ck_agent_tool_proposal_index"),
    sa.CheckConstraint(
        "status IN ('PENDING','RUNNING','COMPLETED','FAILED')",
        name="ck_agent_tool_request_status",
    ),
)
agent_tool_observations = table(
    "agent_tool_observations",
    sa.Column("observation_id", ID, primary_key=True),
    sa.Column(
        "tool_request_id",
        ID,
        sa.ForeignKey("agent_tool_requests.tool_request_id"),
        nullable=False,
        unique=True,
    ),
    sa.Column("status", sa.String(32), nullable=False),
    sa.Column("result", JSONB, nullable=False),
    sa.Column("result_digest", sa.String(71), nullable=False),
    sa.Column("refs", JSONB, nullable=False),
    sa.Column("error_category", sa.String(64)),
    sa.Column("contains_untrusted_data", sa.Boolean, nullable=False),
    sa.Column("observed_at", TIME, nullable=False),
    sa.CheckConstraint(
        "status IN ('SUCCEEDED','DENIED','NOT_FOUND','TEMPORARILY_UNAVAILABLE','FAILED')",
        name="ck_agent_tool_observation_status",
    ),
)
sa.Index(
    "ix_agent_tool_requests_run",
    agent_tool_requests.c.agent_run_id,
    agent_tool_requests.c.created_at,
)

# P6.4 joins an accepted Agent tool request to the business identity returned
# by the existing application use case. The link is recovery evidence; the
# Action, Deliverable, and Acceptance tables remain authoritative.
agent_action_links = table(
    "agent_action_links",
    sa.Column("link_id", ID, primary_key=True),
    sa.Column(
        "tool_request_id",
        ID,
        sa.ForeignKey("agent_tool_requests.tool_request_id"),
        nullable=False,
        unique=True,
    ),
    sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), nullable=False),
    sa.Column("proposal_id", ID, sa.ForeignKey("agent_proposals.proposal_id"), nullable=False),
    sa.Column("operation", sa.String(64), nullable=False),
    sa.Column("business_id", ID, nullable=False),
    sa.Column("action_id", ID, sa.ForeignKey("actions.action_id")),
    sa.Column("command_id", ID, sa.ForeignKey("commands.command_id")),
    sa.Column("request_digest", sa.String(71), nullable=False),
    sa.Column("observation_revision", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("last_observation_digest", sa.String(71)),
    sa.Column("created_at", TIME, nullable=False),
    sa.CheckConstraint(
        "operation IN ('SUBMIT_OPERATOR','SUBMIT_WORKFLOW','REGISTER_DELIVERABLE',"
        "'REQUEST_ACCEPTANCE','CREATE_ANALYSIS','REGISTER_ANALYSIS','CREATE_CLAIM')",
        name="ck_agent_action_link_operation",
    ),
)
sa.Index(
    "ix_agent_action_links_run", agent_action_links.c.agent_run_id, agent_action_links.c.created_at
)

# P6.5 freezes what a WAIT proposal is waiting for. Source events and the
# framework checkpoint are hints; these rows plus the authoritative business
# objects decide whether another model turn is allowed.
agent_wait_dependencies = table(
    "agent_wait_dependencies",
    sa.Column("dependency_id", ID, primary_key=True),
    sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), nullable=False),
    sa.Column("proposal_id", ID, sa.ForeignKey("agent_proposals.proposal_id"), nullable=False),
    sa.Column("dependency_index", sa.Integer, nullable=False),
    sa.Column("owner", sa.String(24), nullable=False),
    sa.Column("object_id", ID, nullable=False),
    sa.Column("minimum_revision", sa.BigInteger),
    sa.Column("observed_revision", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("state", sa.String(16), nullable=False),
    sa.Column("state_digest", sa.String(71), nullable=False),
    sa.Column("created_at", TIME, nullable=False),
    sa.Column("updated_at", TIME, nullable=False),
    sa.UniqueConstraint("proposal_id", "dependency_index", name="uq_agent_wait_position"),
    sa.CheckConstraint(
        "owner IN ('PLATFORM','DATA','KNOWLEDGE','EXECUTION','ACCEPTANCE','MODEL')",
        name="ck_agent_wait_owner",
    ),
    sa.CheckConstraint(
        "state IN ('WAITING','SATISFIED','UNKNOWN','CANCELLED')",
        name="ck_agent_wait_state",
    ),
)
sa.Index(
    "ix_agent_wait_active",
    agent_wait_dependencies.c.agent_run_id,
    agent_wait_dependencies.c.state,
)

agent_source_watermarks = table(
    "agent_source_watermarks",
    sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), primary_key=True),
    sa.Column("source", sa.String(32), primary_key=True),
    sa.Column("stream_id", ID, primary_key=True),
    sa.Column("latest_revision", sa.BigInteger, nullable=False),
    sa.Column("latest_event_digest", sa.String(71), nullable=False),
    sa.Column("updated_at", TIME, nullable=False),
)

agent_runtime_recoveries = table(
    "agent_runtime_recoveries",
    sa.Column("recovery_id", ID, primary_key=True),
    sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), nullable=False),
    sa.Column("from_epoch", sa.Integer, nullable=False),
    sa.Column("to_epoch", sa.Integer, nullable=False),
    sa.Column("reason", sa.String(24), nullable=False),
    sa.Column("ledger_revision", sa.BigInteger, nullable=False),
    sa.Column("ledger_digest", sa.String(71), nullable=False),
    sa.Column("created_at", TIME, nullable=False),
    sa.UniqueConstraint("agent_run_id", "to_epoch", name="uq_agent_recovery_epoch"),
)

agent_reconciliation_timers = table(
    "agent_reconciliation_timers",
    sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), primary_key=True),
    sa.Column("next_attempt_at", TIME, nullable=False),
    sa.Column("failure_count", sa.Integer, nullable=False, server_default="0"),
    sa.Column("last_category", sa.String(64)),
    sa.Column("updated_at", TIME, nullable=False),
)

# P6.7 records every autonomous delivery/repair branch. A new model frame may
# propose a different request, but it cannot erase earlier failures or exceed
# the deterministic branch limit.
agent_delivery_attempts = table(
    "agent_delivery_attempts",
    sa.Column("attempt_id", ID, primary_key=True),
    sa.Column("agent_run_id", ID, sa.ForeignKey("agent_runs.agent_run_id"), nullable=False),
    sa.Column(
        "tool_request_id",
        ID,
        sa.ForeignKey("agent_tool_requests.tool_request_id"),
        nullable=False,
        unique=True,
    ),
    sa.Column("tool_id", sa.String(128), nullable=False),
    sa.Column("branch_key", sa.String(71), nullable=False),
    sa.Column("ordinal", sa.Integer, nullable=False),
    sa.Column("request_digest", sa.String(71), nullable=False),
    sa.Column("outcome", sa.String(24), nullable=False),
    sa.Column("failure_category", sa.String(64)),
    sa.Column("retry_of_attempt_id", ID, sa.ForeignKey("agent_delivery_attempts.attempt_id")),
    sa.Column("stop_reason", sa.String(64)),
    sa.Column("created_at", TIME, nullable=False),
    sa.Column("finished_at", TIME),
    sa.UniqueConstraint("agent_run_id", "branch_key", "ordinal", name="uq_agent_branch_ordinal"),
    sa.CheckConstraint(
        "outcome IN ('RUNNING','SUCCEEDED','FAILED','UNKNOWN','STOPPED')",
        name="ck_agent_delivery_outcome",
    ),
)
sa.Index(
    "ix_agent_delivery_branch",
    agent_delivery_attempts.c.agent_run_id,
    agent_delivery_attempts.c.branch_key,
)
