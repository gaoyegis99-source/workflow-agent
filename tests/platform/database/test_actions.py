import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Barrier

import pytest
import sqlalchemy as sa
from platform_contracts.control import ActionRequest
from platform_core.application.dispatch import Dispatcher
from platform_core.domain.control import ExternalObservation, RuleViolation
from platform_infrastructure import tables as t

from .conftest import TARGET


def action_request(task):
    return ActionRequest.model_validate(
        {
            "task_id": task,
            "expected_spec_revision": 1,
            "target": TARGET,
            "resources": [],
            "parameters": {"value": 7},
        }
    ).model_dump(mode="json")


def claim_action(database, owner="worker"):
    dispatcher = Dispatcher(database)
    for _ in range(100):
        claim = dispatcher.claim(owner)
        if claim is None:
            return None
        if claim["kind"] == "ACTION":
            return claim
        dispatcher.prepare(claim)
    raise AssertionError("No action after draining task events")


def available(database):
    with database.engine.begin() as connection:
        connection.execute(
            t.outbox.update().where(t.outbox.c.done.is_(False)).values(available_at=sa.func.now())
        )


@pytest.mark.parametrize(
    "setup_task",
    [
        {"execution_count": 1, "max_in_flight": 8},
        {"execution_count": 8, "max_in_flight": 1},
    ],
    indirect=True,
)
def test_real_connections_cannot_overspend_last_quota(database, setup_task):
    app, actor, _, project, task, _ = setup_task
    barrier = Barrier(12)

    def submit(index):
        barrier.wait()
        try:
            return app.submit_action(actor, project, str(index), action_request(task))["action_id"]
        except RuleViolation as error:
            assert error.code == "BUDGET_BLOCKED"
            return None

    with ThreadPoolExecutor(12) as pool:
        results = list(pool.map(submit, range(12)))
    assert sum(result is not None for result in results) == 1
    budget = app.budget(actor, project, task)
    assert budget["reserved"] == budget["in_flight"] == 1 and budget["used"] == 0
    with database.engine.connect() as connection:
        for table in [t.actions, t.reservations, t.commands]:
            assert connection.scalar(sa.select(sa.func.count()).select_from(table)) == 1
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(t.outbox).where(t.outbox.c.kind == "ACTION")
            )
            == 1
        )


def test_concurrent_replay_has_one_identity_and_one_reservation(database, setup_task):
    app, actor, _, project, task, _ = setup_task
    barrier = Barrier(12)

    def submit(_):
        barrier.wait()
        return app.submit_action(actor, project, "same", action_request(task))

    with ThreadPoolExecutor(12) as pool:
        results = list(pool.map(submit, range(12)))
    assert all(item == results[0] for item in results)
    assert app.budget(actor, project, task)["reserved"] == 1
    with pytest.raises(RuleViolation) as error:
        app.submit_action(
            actor, project, "same", action_request(task) | {"parameters": {"value": 8}}
        )
    assert error.value.code == "IDEMPOTENCY_CONFLICT"


def test_pause_then_resume_and_revision_rechecks_before_send(database, setup_task):
    app, actor, _, project, task, _ = setup_task
    command = app.submit_action(actor, project, "a", action_request(task))
    app.control(actor, project, task, "pause", "pause", 1)
    dispatcher = Dispatcher(database)
    assert dispatcher.prepare(claim_action(database)) is None
    assert app.budget(actor, project, task)["reserved"] == 1
    app.control(actor, project, task, "resume", "resume", 2)
    app.revise(
        actor,
        project,
        task,
        "revision",
        {
            "expected_control_version": 3,
            "expected_spec_revision": 1,
            "spec": {"goal": "new", "requirements": []},
        },
    )
    available(database)
    assert dispatcher.prepare(claim_action(database)) is None
    assert app.command(actor, project, command["command_id"])["error_code"] == "REVISION_CONFLICT"
    assert app.budget(actor, project, task)["reserved"] == 0
    with pytest.raises(RuleViolation) as error:
        app.submit_action(actor, project, "stale", action_request(task))
    assert error.value.code == "REVISION_CONFLICT"


@pytest.mark.parametrize("reason", ["cancel", "grant", "token"])
def test_recheck_cancellation_and_authorization_before_first_send(database, setup_task, reason):
    app, actor, _, project, task, grant = setup_task
    command = app.submit_action(actor, project, "a", action_request(task))
    if reason == "cancel":
        app.control(actor, project, task, "cancel", "cancel", 1)
    elif reason == "grant":
        app.revoke_grant(actor, project, grant["grant_id"], "revoke")
    else:
        database.revoke_token(actor.token_id)
    dispatcher = Dispatcher(database)
    assert dispatcher.prepare(claim_action(database)) is None
    with database.transaction() as uow:
        action = uow.actions.get(command["action_id"])
        assert action["stopped"] and not action["send_started"]
        budget = uow.budgets.get(task)
        assert budget.used == budget.reserved == budget.in_flight == 0


def test_unknown_cancel_retains_reservation_until_durable_seal(database, setup_task):
    app, actor, _, project, task, _ = setup_task
    command = app.submit_action(actor, project, "a", action_request(task))
    dispatcher = Dispatcher(database)
    claim = claim_action(database)
    assert dispatcher.prepare(claim)["method"] == "submit"
    app.control(actor, project, task, "cancel", "cancel", 1)
    dispatcher.uncertain(claim)
    assert app.task(actor, project, task)["cleanup_status"] == "UNKNOWN"
    assert app.budget(actor, project, task)["reserved"] == 1
    available(database)
    claim = claim_action(database)
    assert dispatcher.prepare(claim)["method"] == "cancel"
    dispatcher.observe(
        claim, ExternalObservation("seal-1", command["action_id"], 1, "SEALED", None, False)
    )
    assert (
        app.budget(actor, project, task)["reserved"]
        == app.budget(actor, project, task)["in_flight"]
        == 0
    )
    assert app.task(actor, project, task)["cleanup_status"] == "CLEAR"


def test_duplicate_out_of_order_and_conflicting_observations(database, setup_task):
    app, actor, _, project, task, _ = setup_task
    command = app.submit_action(actor, project, "a", action_request(task))
    dispatcher = Dispatcher(database)
    claim = claim_action(database)
    dispatcher.prepare(claim)
    running = ExternalObservation("running-2", command["action_id"], 2, "RUNNING", "peer-1", True)
    dispatcher.observe(claim, running)
    assert app.budget(actor, project, task)["used"] == 1
    for observation in [running, replace(running, event_id="running-1", revision=1)]:
        available(database)
        dispatcher.observe(claim_action(database), observation)
        assert app.budget(actor, project, task)["used"] == 1
    available(database)
    conflict = claim_action(database)
    with pytest.raises(RuleViolation, match="conflicting"):
        dispatcher.observe(conflict, replace(running, state="SUCCEEDED"))
    # The conflicting event transaction rolled back, retaining the claim for a valid reply.
    dispatcher.observe(conflict, replace(running, event_id="done-3", revision=3, state="SUCCEEDED"))
    budget = app.budget(actor, project, task)
    assert (budget["used"], budget["reserved"], budget["in_flight"]) == (1, 0, 0)
    assert app.task(actor, project, task)["lifecycle"] == "OPEN"
    assert app.task(actor, project, task)["control_version"] == 1
    with database.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(t.settlements)) == 2


def test_transient_backoff_tracks_failures_and_resets_after_observation(database, setup_task):
    app, actor, _, project, task, _ = setup_task
    command = app.submit_action(actor, project, "backoff", action_request(task))
    dispatcher = Dispatcher(database)
    for interval in [1, 2, 4, 8, 16, 30, 30]:
        available(database)
        claim = claim_action(database)
        dispatcher.prepare(claim)
        dispatcher.uncertain(claim)
        with database.engine.connect() as connection:
            delay = connection.scalar(
                sa.select(
                    sa.extract("epoch", t.outbox.c.available_at - sa.func.clock_timestamp())
                ).where(t.outbox.c.event_id == claim["event_id"])
            )
        assert interval - 0.5 < delay <= interval
    available(database)
    dispatcher.observe(
        claim_action(database),
        ExternalObservation("accepted", command["action_id"], 1, "RUNNING", "peer", True),
    )
    available(database)
    claim = claim_action(database)
    assert dispatcher.prepare(claim, absent=True) is None
    with database.engine.connect() as connection:
        action = connection.execute(sa.select(t.actions)).mappings().one()
        assert action["state"] == "RECONCILING" and action["failure_count"] == 1
    assert app.budget(actor, project, task)["in_flight"] == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"action_id": "wrong"},
        {"revision": True},
        {"executed": 1},
        {"state": "SEALED"},
        {"state": "INVALID"},
        {"execution_id": None},
    ],
)
def test_invalid_external_evidence_cannot_settle_budget(database, setup_task, changes):
    app, actor, _, project, task, _ = setup_task
    command = app.submit_action(actor, project, "evidence", action_request(task))
    dispatcher = Dispatcher(database)
    claim = claim_action(database)
    dispatcher.prepare(claim)
    result = ExternalObservation("evidence", command["action_id"], 1, "RUNNING", "peer", True)
    with pytest.raises(RuleViolation):
        dispatcher.observe(claim, replace(result, **changes))
    assert app.budget(actor, project, task)["reserved"] == 1


def test_lease_expiry_fences_stale_writer_and_renewer(database, setup_task):
    app, actor, _, project, task, _ = setup_task
    command = app.submit_action(actor, project, "a", action_request(task))
    dispatcher = Dispatcher(database)
    old = claim_action(database, "old")
    dispatcher.prepare(old)
    with database.engine.begin() as connection:
        connection.execute(
            t.outbox.update()
            .where(t.outbox.c.event_id == old["event_id"])
            .values(lease_until=sa.func.now())
        )
    new = claim_action(database, "new")
    assert new["generation"] == old["generation"] + 1
    assert not dispatcher.renew(old)
    result = ExternalObservation("complete", command["action_id"], 2, "SUCCEEDED", "peer-1", True)
    assert not dispatcher.observe(old, result)
    assert app.budget(actor, project, task)["reserved"] == 1
    assert dispatcher.observe(new, result)
    assert app.budget(actor, project, task)["used"] == 1


def test_lease_expiring_while_waiting_for_row_lock_cannot_authorize_write(database, setup_task):
    from .processes import eventually

    app, actor, _, project, task, _ = setup_task
    command = app.submit_action(actor, project, "blocked-lease", action_request(task))
    dispatcher = Dispatcher(database)
    claim = claim_action(database)
    dispatcher.prepare(claim)
    with database.engine.begin() as connection:
        connection.execute(
            t.outbox.update()
            .where(t.outbox.c.event_id == claim["event_id"])
            .values(lease_until=sa.func.clock_timestamp() + timedelta(seconds=0.6))
        )
    blocker = database.engine.connect()
    transaction = blocker.begin()
    blocker.execute(
        sa.select(t.outbox).where(t.outbox.c.event_id == claim["event_id"]).with_for_update()
    )
    result = ExternalObservation("late", command["action_id"], 1, "RUNNING", "peer", True)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(dispatcher.observe, claim, result)
        try:

            def waiting():
                with database.engine.connect() as connection:
                    return (
                        connection.scalar(
                            sa.text(
                                "SELECT count(*) FROM pg_stat_activity "
                                "WHERE wait_event_type='Lock' "
                                "AND query LIKE '%outbox%'"
                            )
                        )
                        > 0
                    )

            eventually(waiting)
            time.sleep(0.7)
        finally:
            transaction.rollback()
            blocker.close()
        assert future.result(timeout=5) is False
    assert app.budget(actor, project, task)["reserved"] == 1


def test_definitive_external_rejection_releases_once_and_finishes_delivery(database, setup_task):
    app, actor, _, project, task, _ = setup_task
    command = app.submit_action(actor, project, "rejected", action_request(task))
    dispatcher = Dispatcher(database)
    claim = claim_action(database)
    dispatcher.prepare(claim)
    rejection = ExternalObservation("rejected", command["action_id"], 1, "REJECTED", None, False)
    assert dispatcher.observe(claim, rejection)
    assert app.command(actor, project, command["command_id"])["status"] == "REJECTED"
    assert app.budget(actor, project, task)["in_flight"] == 0
    assert app.budget(actor, project, task)["used"] == 0
    assert not dispatcher.observe(claim, rejection)
    assert claim_action(database) is None
