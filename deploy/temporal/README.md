# V7 Temporal single-machine profile

This profile runs a self-hosted Temporal Service, a dedicated PostgreSQL
database, Temporal UI bound to localhost by default, and the V7 general
workflow Worker.  It is intended for the first V7 reliability milestone:
process/Worker failure and restart recovery on one machine.  It does **not**
claim multi-host or no-interruption high availability.

## Release inputs

Generate the following outside version control, with restrictive permissions:

| Input | Contents |
| --- | --- |
| `V7_TEMPORAL_*_IMAGE` | Immutable release image references (prefer digest-pinned references) |
| `V7_TEMPORAL_POSTGRES_DIR` | Dedicated local PostgreSQL data directory |
| `V7_TEMPORAL_POSTGRES_ENV_FILE` | `POSTGRES_PASSWORD` only |
| `V7_TEMPORAL_SERVER_ENV_FILE` | Temporal server's `POSTGRES_PWD` only |
| `V7_TEMPORAL_WORKER_ENV_FILE` | `V7_TEMPORAL_GOVERNANCE_URL`, factory references, non-secret worker configuration |
| `V7_TEMPORAL_SECRETS_DIR/governance-token` | The private platform-to-worker token, mode 0600 |
| `V7_TEMPORAL_PLUGIN_MANIFESTS_DIR/plugins.json` | Reviewed exact `PluginManifest` array |
| `V7_PLATFORM_INTERNAL_NETWORK` | Existing V7 private Docker network shared with the Platform API listener |

The Worker’s default governance factory may be
`platform_temporal_workflows.http_gateway:http_ledger_from_environment`.  It
talks only to the V7 platform's private `/internal/temporal/v1` endpoint and
does not open a database connection.  A deployment-specific plugin-host factory
must return reviewed process/container/remote adapters; model output cannot
name a Python module or install a plugin.

For a process-isolated plugin, a deployment factory can construct
`ProcessPluginHost` and register `ProcessPluginDefinition` entries from
reviewed release configuration. Each entry fixes an absolute executable and
non-secret environment for one exact manifest; workflow arguments travel only
over the host's JSON stdin/stdout protocol. Do not pass a Platform database URL
or an unrestricted inherited environment to that factory.

The Platform API mounts
`platform_temporal_workflows.create_governance_router(existing_ledger, token)`
on that private network.  `existing_ledger` is the production adapter to the
V7 transactional action/budget/outbox ledger; the Worker receives only the
HTTP contract and its mounted token.  The checked-in router deliberately does
not create a second database or expose itself through the public gateway.

The profile uses Temporal's `default` namespace by default, which works on a
new auto-setup service.  If `V7_TEMPORAL_NAMESPACE` is changed, provision that
namespace with Temporal administration tooling before starting the Worker.

Start only after image/config validation:

```bash
docker compose --env-file /secure/v7-temporal/compose.env \
  -f deploy/temporal/compose.yaml config
docker compose --env-file /secure/v7-temporal/compose.env \
  -f deploy/temporal/compose.yaml up -d
```

For development and tests, use `temporal server start-dev` or the Python SDK
time-skipping environment; do not use a development server as the production
runtime.

## Operational contract

- The platform ledger owns action identity, permission and budget decisions,
  output acceptance, and outbox events. Temporal owns timers, signal waits,
  Activity retry scheduling, child workflows, and replay.
- Platform API/Webhook handlers persist a frozen `TemporalDispatchCommand`
  into the existing outbox before any Temporal call. An outbox Worker uses
  `TemporalOutboxDispatcher`; it must retain the same command until the
  corresponding start, schedule, approval, reconciliation, or cancellation
  signal is delivered.
- Manual/API start and schedule creation require a stable `Idempotency-Key` at
  the public trigger boundary. The authoritative Platform transaction maps a
  retry to the original frozen run and outbox command; webhook delivery IDs
  are instead verified and deduplicated by the provider-aware webhook service.

`SQLiteDevelopmentWorkflowPlatform` exists only for local contract tests. It
persists a frozen invocation/audit fact/dispatch command and leases it with a
generation, but it is not the production Platform database or an alternative
to its existing transactional action/budget/outbox ledger.
- A worker process loss may repeat an Activity. `action_id` and
  `idempotency_key` remain stable, and an Activity with an unknown external
  result becomes `RECONCILING` rather than issuing a fresh external request.
- Approval is a Temporal signal plus a persisted platform event; it does not
  poll a model. Cancellation records intent separately from an external stop;
  compensation is an explicit workflow/plugin action.
- Deploy new workflow code alongside old Worker versions until every old
  execution closes. Stop assigning new runs to an old backend before rollback;
  do not migrate in-flight V6/V7 checkpoints or writable ledgers.

## Required failure drill

Before release, exercise a worker kill while a workflow is waiting for
approval, restart the Worker, signal approval, and verify that the original
`action_id` and reservation are settled once.  Also exercise duplicate Activity
delivery, successful external call with missing receipt, cancellation timeout,
permission revocation, and a late old-worker result.  The automated contract
tests cover the ledger/idempotency portions; this profile's drill verifies the
actual compose process boundary.
