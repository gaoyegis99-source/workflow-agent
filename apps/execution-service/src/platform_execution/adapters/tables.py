import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

metadata = sa.MetaData()
executions = sa.Table(
    "executions",
    metadata,
    sa.Column("action_id", sa.String(128), primary_key=True),
    sa.Column("execution_id", sa.String(128), nullable=False, unique=True),
    sa.Column("parent_execution_id", sa.String(128), sa.ForeignKey("executions.execution_id")),
    sa.Column("invocation_key", sa.String(128)),
    sa.Column("project_id", sa.String(128), nullable=False),
    sa.Column("task_id", sa.String(128), nullable=False),
    sa.Column("digest", sa.String(64), nullable=False),
    sa.Column("payload", JSONB, nullable=False),
    sa.Column("state", sa.String(24), nullable=False),
    sa.Column("revision", sa.BigInteger, nullable=False),
    sa.Column("executed", sa.Boolean, nullable=False),
    sa.Column("cancel_requested", sa.Boolean, nullable=False),
    sa.Column("attempt_id", sa.String(128), nullable=False),
    sa.Column("container_id", sa.String(128)),
    sa.Column("runtime", JSONB, nullable=False),
    sa.Column("work_class", sa.String(32), nullable=False, server_default="NODE_EXECUTION"),
    sa.Column("owner", sa.String(128)),
    sa.Column("generation", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("lease_until", sa.DateTime(timezone=True)),
    sa.Column(
        "available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
    ),
)
sa.CheckConstraint(
    executions.c.work_class.in_(("WORKFLOW_SUPERVISOR", "NODE_EXECUTION", "PUBLICATION_RECONCILE")),
    name="execution_work_class_valid",
)
sa.Index(
    "execution_work_ready",
    executions.c.work_class,
    executions.c.available_at,
    postgresql_where=executions.c.state.not_in(
        ("SUCCEEDED", "FAILED", "CANCELLED", "REJECTED", "SEALED")
    ),
)
sa.UniqueConstraint(
    executions.c.parent_execution_id,
    executions.c.invocation_key,
    name="execution_invocation",
)

workflow_trace_events = sa.Table(
    "workflow_trace_events",
    metadata,
    sa.Column("event_key", sa.String(64), primary_key=True),
    sa.Column(
        "workflow_execution_id",
        sa.String(128),
        sa.ForeignKey("executions.execution_id"),
        nullable=False,
    ),
    sa.Column("node_action_id", sa.String(128), sa.ForeignKey("executions.action_id")),
    sa.Column("engine_epoch", sa.Integer),
    sa.Column("worker_generation", sa.BigInteger),
    sa.Column("source", sa.String(32), nullable=False),
    sa.Column("event_type", sa.String(48), nullable=False),
    sa.Column("source_monotonic_ns", sa.BigInteger),
    sa.Column("duration_ms", sa.Float),
    sa.Column("attributes", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column(
        "observed_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.clock_timestamp(),
    ),
)

remote_calls = sa.Table(
    "remote_calls",
    metadata,
    sa.Column("action_id", sa.String(128), sa.ForeignKey("executions.action_id"), primary_key=True),
    sa.Column("binding", JSONB, nullable=False),
    sa.Column("request_digest", sa.String(71), nullable=False),
    sa.Column("phase", sa.String(32), nullable=False),
    sa.Column("remote_execution_id", sa.String(128)),
    sa.Column("remote_revision", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("remote_state", sa.String(24)),
    sa.Column("executed", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("observation", JSONB),
    sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.clock_timestamp(),
    ),
)
remote_observations = sa.Table(
    "remote_observations",
    metadata,
    sa.Column(
        "action_id", sa.String(128), sa.ForeignKey("remote_calls.action_id"), primary_key=True
    ),
    sa.Column("event_id", sa.String(256), primary_key=True),
    sa.Column("revision", sa.BigInteger, nullable=False),
    sa.Column("observation_digest", sa.String(71), nullable=False),
    sa.Column("observation", JSONB, nullable=False),
    sa.Column(
        "observed_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.clock_timestamp(),
    ),
)
sa.Index(
    "workflow_trace_parent_time",
    workflow_trace_events.c.workflow_execution_id,
    workflow_trace_events.c.observed_at,
)
sa.Index(
    "workflow_trace_node_time",
    workflow_trace_events.c.node_action_id,
    workflow_trace_events.c.observed_at,
)
