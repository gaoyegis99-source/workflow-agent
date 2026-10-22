from datetime import timedelta

import pytest
import sqlalchemy as sa
from platform_core.application.agent_kernel import AgentCoordinator
from platform_core.application.agent_waiting import AgentWaitReconciler
from platform_core.domain.agent_runtime import DecisionProposal, canonical_digest
from platform_core.domain.control import RuleViolation
from platform_infrastructure import database as database_module


def wait(frame, owner, object_id, minimum_revision=None):
    dependency = {"owner": owner, "object_id": object_id}
    if minimum_revision is not None:
        dependency["minimum_revision"] = minimum_revision
    return DecisionProposal(
        "WAIT_REQUEST",
        frame.frame_id,
        frame.task_spec_revision,
        {"reason": "RECONCILIATION", "dependencies": [dependency]},
    )


def test_schema_contains_wait_recovery_ledger(database):
    assert database_module.SCHEMA_VERSION == "0023_workflow_release_lifecycle"
    with database.engine.connect() as connection:
        version = connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
        tables = {
            row[0]
            for row in connection.execute(
                sa.text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name LIKE 'agent_%'"
                )
            )
        }
    assert version == "0023_workflow_release_lifecycle"
    assert {
        "agent_wait_dependencies",
        "agent_source_watermarks",
        "agent_runtime_recoveries",
        "agent_reconciliation_timers",
    } <= tables


def test_wait_rejects_identity_not_observed_for_this_agent(agent_setup):
    coordinator = AgentCoordinator(agent_setup["database"])
    claim = coordinator.claim("p65-invalid-wait", 30)
    frame = coordinator.prepare_frame(claim)
    with pytest.raises(RuleViolation) as error:
        coordinator.accept_proposal(claim, frame, wait(frame, "EXECUTION", "someone-elses-action"))
    assert error.value.code == "INVALID_BINDING"
    with agent_setup["database"].transaction() as uow:
        assert uow.agents.proposals(agent_setup["agent_run_id"]) == []
        assert uow.agents.wait_dependencies(agent_setup["agent_run_id"]) == []


def test_event_first_wait_and_lost_wakeup_reconcile_without_model_polling(agent_setup):
    coordinator = AgentCoordinator(agent_setup["database"])
    assert coordinator.register_event(
        agent_setup["project_id"],
        agent_setup["task"]["task_id"],
        source="DATA",
        event_id="data-event-one",
        stream_id="data-import-one",
        source_revision=1,
        kind="IMPORT_RUNNING",
        payload={"status": "RUNNING"},
    )
    claim = coordinator.claim("p65-wait-worker", 30)
    frame = coordinator.prepare_frame(claim)
    accepted = coordinator.accept_proposal(claim, frame, wait(frame, "DATA", "data-import-one", 2))
    with agent_setup["database"].transaction() as uow:
        run = uow.agents.run_by_id(agent_setup["agent_run_id"])
        dependencies = uow.agents.wait_dependencies(run["agent_run_id"])
    assert accepted["kind"] == "WAIT_REQUEST"
    assert run["status"] == "WAITING"
    assert dependencies[0]["state"] == "WAITING"

    # Simulate a committed source revision whose notification was lost. The
    # timer observes the watermark and creates one wake without provider I/O.
    with agent_setup["database"].transaction() as uow:
        now = uow.now()
        uow.agents.update_watermark(
            {
                "agent_run_id": run["agent_run_id"],
                "source": "DATA",
                "stream_id": "data-import-one",
                "latest_revision": 2,
                "latest_event_digest": canonical_digest({"revision": 2}),
                "updated_at": now,
            }
        )
        uow.agents.schedule_reconciliation(
            run["agent_run_id"], now - timedelta(seconds=1), category="LOST_EVENT"
        )
    reconciler = AgentWaitReconciler(agent_setup["database"])
    assert reconciler.process()
    assert not reconciler.process()
    with agent_setup["database"].transaction() as uow:
        run = uow.agents.run_by_id(agent_setup["agent_run_id"])
        dependencies = uow.agents.wait_dependencies(run["agent_run_id"])
        wakes = uow.agents.pending_wakeups(run["agent_run_id"])
    assert run["status"] == "READY"
    assert dependencies[0]["state"] == "SATISFIED"
    assert len(wakes) == 1


def test_runtime_recovery_records_epoch_and_ledger_digest(agent_setup):
    coordinator = AgentCoordinator(agent_setup["database"])
    claim = coordinator.claim("p65-recovery-worker", 30)
    receipt = coordinator.recover_runtime_from_ledger(claim, "MISSING")
    with agent_setup["database"].transaction() as uow:
        recoveries = uow.agents.runtime_recoveries(agent_setup["agent_run_id"])
        run = uow.agents.run_by_id(agent_setup["agent_run_id"])
    assert receipt["recovery_id"] == recoveries[0]["recovery_id"]
    assert (recoveries[0]["from_epoch"], recoveries[0]["to_epoch"]) == (1, 2)
    assert recoveries[0]["ledger_digest"].startswith("sha256:")
    assert run["epoch"] == 2 and run["runtime_ref"].endswith("-e2")


def test_admin_checkpoint_rebuild_preserves_wait_state_and_rejects_active_worker(agent_setup):
    coordinator = AgentCoordinator(agent_setup["database"])
    claim = coordinator.claim("p65-admin-race", 30)
    with pytest.raises(RuleViolation) as error:
        coordinator.request_checkpoint_rebuild(agent_setup["agent_run_id"])
    assert error.value.code == "REVISION_CONFLICT"
    coordinator.release_transient(claim)

    with agent_setup["database"].transaction() as uow:
        uow.agents.update_run(agent_setup["agent_run_id"], status="WAITING")
    receipt = coordinator.request_checkpoint_rebuild(agent_setup["agent_run_id"])
    with agent_setup["database"].transaction() as uow:
        run = uow.agents.run_by_id(agent_setup["agent_run_id"])
        recovery = uow.agents.runtime_recoveries(agent_setup["agent_run_id"])[-1]
    assert receipt["reason"] == "ADMIN_REQUEST"
    assert receipt["status"] == "WAITING"
    assert run["status"] == "WAITING"
    assert run["runtime_ref"].endswith("-e2")
    assert recovery["reason"] == "ADMIN_REQUEST"
