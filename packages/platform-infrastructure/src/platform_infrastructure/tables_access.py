"""Access, task-control, action, and delivery table declarations."""

from .tables_shared import ID, JSONB, TIME, sa, table

principals = table(
    "principals",
    sa.Column("principal_id", ID, primary_key=True),
    sa.Column("name", sa.String(200), nullable=False),
    # An optional stable subject owned by the enterprise identity provider.
    # Project roles remain in ``members``; this field is only identity binding.
    sa.Column("external_subject", sa.String(128), nullable=True),
    sa.UniqueConstraint("external_subject", name="uq_principals_external_subject"),
)
principal_identity_events = table(
    "principal_identity_events",
    sa.Column("event_id", ID, primary_key=True),
    sa.Column("principal_id", ID, sa.ForeignKey("principals.principal_id"), nullable=False),
    sa.Column("previous_subject", sa.String(128)),
    sa.Column("bound_subject", sa.String(128), nullable=False),
    sa.Column("reviewer", sa.String(128), nullable=False),
    sa.Column("reason", sa.String(512), nullable=False),
    sa.Column("created_at", TIME, nullable=False, server_default=sa.func.now()),
)
tokens = table(
    "tokens",
    sa.Column("token_id", ID, primary_key=True),
    sa.Column("principal_id", ID, sa.ForeignKey("principals.principal_id"), nullable=False),
    sa.Column("digest", sa.String(64), unique=True, nullable=False),
    sa.Column("expires_at", TIME, nullable=False),
    sa.Column("revoked", sa.Boolean, nullable=False, server_default=sa.false()),
)
projects = table(
    "projects",
    sa.Column("project_id", ID, primary_key=True),
    sa.Column("name", sa.String(200), nullable=False),
    sa.Column("created_at", TIME, nullable=False, server_default=sa.func.now()),
)
members = table(
    "members",
    sa.Column("project_id", ID, sa.ForeignKey("projects.project_id"), primary_key=True),
    sa.Column("principal_id", ID, sa.ForeignKey("principals.principal_id"), primary_key=True),
    sa.Column("role", sa.String(16), nullable=False),
    sa.CheckConstraint("role IN ('OWNER','EDITOR','VIEWER')"),
)
tasks = table(
    "tasks",
    sa.Column("task_id", ID, primary_key=True),
    sa.Column("project_id", ID, sa.ForeignKey("projects.project_id"), nullable=False),
    sa.Column("spec_revision", sa.Integer, nullable=False),
    sa.Column("control_version", sa.Integer, nullable=False),
    sa.Column("lifecycle", sa.String(16), nullable=False),
    sa.Column("control", sa.String(16), nullable=False),
    sa.Column("cleanup_status", sa.String(16), nullable=False),
    sa.Column("grant_epoch", sa.Integer, nullable=False),
    sa.Column("created_at", TIME, nullable=False),
    sa.CheckConstraint("spec_revision > 0 AND control_version > 0 AND grant_epoch > 0"),
    sa.CheckConstraint("lifecycle IN ('OPEN','CANCELLED','COMPLETED')"),
    sa.CheckConstraint("control IN ('ACTIVE','PAUSED')"),
    sa.CheckConstraint("cleanup_status IN ('CLEAR','PENDING','UNKNOWN')"),
)
specs = table(
    "task_specs",
    sa.Column("task_id", ID, sa.ForeignKey("tasks.task_id"), primary_key=True),
    sa.Column("revision", sa.Integer, primary_key=True),
    sa.Column("spec", JSONB, nullable=False),
    sa.Column("actor_id", ID, sa.ForeignKey("principals.principal_id"), nullable=False),
)
grants = table(
    "grants",
    sa.Column("grant_id", ID, primary_key=True),
    sa.Column("project_id", ID, sa.ForeignKey("projects.project_id"), nullable=False),
    sa.Column("task_id", ID, sa.ForeignKey("tasks.task_id"), nullable=False),
    sa.Column("targets", JSONB, nullable=False),
    sa.Column("resources", JSONB, nullable=False),
    sa.Column("expires_at", TIME, nullable=False),
    sa.Column("revoked", sa.Boolean, nullable=False),
    sa.Column("created_by", ID, sa.ForeignKey("principals.principal_id"), nullable=False),
    sa.Column("generation", sa.Integer, nullable=False),
    sa.Column("derived_outputs", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("acceptance_profiles", JSONB, nullable=False, server_default="[]"),
    sa.Column("revoked_at", TIME),
    sa.Column("revoked_by", ID, sa.ForeignKey("principals.principal_id")),
)
budgets = table(
    "budgets",
    sa.Column("task_id", ID, sa.ForeignKey("tasks.task_id"), primary_key=True),
    *[
        sa.Column(name, sa.BigInteger, nullable=False)
        for name in ["execution_count", "max_in_flight", "used", "reserved", "in_flight"]
    ],
    sa.CheckConstraint("execution_count >= 0 AND max_in_flight > 0"),
    sa.CheckConstraint("used >= 0 AND reserved >= 0 AND in_flight >= 0"),
    sa.CheckConstraint("used + reserved <= execution_count AND in_flight <= max_in_flight"),
)
commands = table(
    "commands",
    sa.Column("command_id", ID, primary_key=True),
    sa.Column("project_id", ID, sa.ForeignKey("projects.project_id"), nullable=False),
    sa.Column("task_id", ID, sa.ForeignKey("tasks.task_id"), nullable=False),
    sa.Column("spec_revision", sa.Integer, nullable=False),
    sa.Column("status", sa.String(24), nullable=False),
    sa.Column("action_id", ID),
    sa.Column("execution_id", ID),
    sa.Column("error_code", sa.String(64)),
    sa.Column("error_message", sa.String(512)),
)
actions = table(
    "actions",
    sa.Column("action_id", ID, primary_key=True),
    sa.Column("command_id", ID, sa.ForeignKey("commands.command_id"), unique=True, nullable=False),
    sa.Column("task_id", ID, sa.ForeignKey("tasks.task_id"), nullable=False),
    sa.Column("project_id", ID, sa.ForeignKey("projects.project_id"), nullable=False),
    sa.Column("spec_revision", sa.Integer, nullable=False),
    sa.Column("grant_epoch", sa.Integer, nullable=False),
    sa.Column("principal_id", ID, sa.ForeignKey("principals.principal_id"), nullable=False),
    sa.Column("token_id", ID, sa.ForeignKey("tokens.token_id"), nullable=True),
    sa.Column("frame_id", ID, unique=True, nullable=False),
    sa.Column("decision_source", sa.String(16), nullable=False),
    sa.CheckConstraint("decision_source IN ('CLIENT','AGENT')", name="ck_actions_decision_source"),
    sa.Column("request", JSONB, nullable=False),
    sa.Column("send_started", sa.Boolean, nullable=False),
    sa.Column("state", sa.String(24), nullable=False),
    sa.Column("external_revision", sa.BigInteger, nullable=False),
    sa.Column("execution_id", ID),
    sa.Column("stopped", sa.Boolean, nullable=False),
    sa.Column("cancel_requested", sa.Boolean, nullable=False),
    sa.Column("failure_count", sa.Integer, nullable=False, server_default="0"),
    sa.Column("last_external_observation", JSONB, nullable=True),
)
reservations = table(
    "reservations",
    sa.Column("action_id", ID, sa.ForeignKey("actions.action_id"), primary_key=True),
    sa.Column("task_id", ID, sa.ForeignKey("tasks.task_id"), nullable=False),
    sa.Column("charged", sa.Boolean, nullable=False),
    sa.Column("stopped", sa.Boolean, nullable=False),
)
settlements = table(
    "budget_settlements",
    sa.Column("action_id", ID, sa.ForeignKey("actions.action_id"), primary_key=True),
    sa.Column("source", sa.String(32), primary_key=True),
    sa.Column("event_id", ID, primary_key=True),
    sa.Column("charged", sa.Boolean, nullable=False),
    sa.Column("stopped", sa.Boolean, nullable=False),
    sa.Column("recorded_at", TIME, nullable=False, server_default=sa.func.now()),
)
requests = table(
    "idempotency",
    sa.Column("principal_id", ID, sa.ForeignKey("principals.principal_id"), primary_key=True),
    sa.Column("scope", ID, primary_key=True),
    sa.Column("operation", sa.String(100), primary_key=True),
    sa.Column("key", sa.String(128), primary_key=True),
    sa.Column("digest", sa.String(64), nullable=False),
    sa.Column("result", JSONB),
)
outbox = table(
    "outbox",
    sa.Column("event_id", ID, primary_key=True),
    sa.Column("task_id", ID, sa.ForeignKey("tasks.task_id"), nullable=False),
    sa.Column("kind", sa.String(32), nullable=False),
    sa.Column("payload", JSONB, nullable=False),
    sa.Column("done", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("available_at", TIME, nullable=False, server_default=sa.func.now()),
    sa.Column("owner", ID),
    sa.Column("lease_until", TIME),
    sa.Column("generation", sa.BigInteger, nullable=False, server_default="0"),
    sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
)
inbox = table(
    "inbox",
    sa.Column("source", sa.String(32), primary_key=True),
    sa.Column("event_id", ID, primary_key=True),
    sa.Column("digest", sa.String(64), nullable=False),
)
audit = table(
    "task_events",
    sa.Column("event_id", ID, primary_key=True),
    sa.Column("task_id", ID, sa.ForeignKey("tasks.task_id"), nullable=False),
    sa.Column("payload", JSONB, nullable=False),
)
sa.Index("outbox_ready", outbox.c.available_at, postgresql_where=outbox.c.done.is_(False))
sa.Index("actions_task_active", actions.c.task_id, postgresql_where=actions.c.stopped.is_(False))
sa.Index("tasks_project_page", tasks.c.project_id, tasks.c.task_id)
