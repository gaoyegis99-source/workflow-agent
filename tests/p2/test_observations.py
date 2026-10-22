from dataclasses import asdict, replace

import pytest
import sqlalchemy as sa
from platform_core.application.dispatch import Dispatcher
from platform_core.domain.control import ExternalObservation, RuleViolation
from platform_infrastructure import tables as t

from .test_actions import action_request, available, claim_action
from .test_recovery import row


def running(database, setup_task):
    app, actor, _, project, task, _ = setup_task
    command = app.submit_action(actor, project, "observation", action_request(task))
    dispatcher = Dispatcher(database)
    claim = claim_action(database)
    dispatcher.prepare(claim)
    observation = ExternalObservation("event:2", command["action_id"], 2, "RUNNING", "peer-1", True)
    dispatcher.observe(claim, observation)
    available(database)
    dispatcher.uncertain(claim_action(database))
    return dispatcher, command, observation


@pytest.mark.parametrize("different_event", [False, True])
def test_same_facts_recover_and_cancel_remains_pending(database, setup_task, different_event):
    app, actor, _, project, task, _ = setup_task
    dispatcher, command, observation = running(database, setup_task)
    previous = row(database, command["action_id"])["last_external_observation"]
    assert previous == {k: v for k, v in asdict(observation).items() if k != "event_id"}
    app.control(actor, project, task, "cancel", "cancel", 1)
    available(database)
    if different_event:
        observation = replace(observation, event_id="another:event")
    dispatcher.observe(claim_action(database), observation)
    assert row(database, command["action_id"])["state"] == "RUNNING"
    assert app.task(actor, project, task)["cleanup_status"] == "PENDING"
    assert app.budget(actor, project, task)["in_flight"] == 1
    with database.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(t.settlements)) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"event_id": "new", "state": "SUCCEEDED"},
        {"event_id": "new", "execution_id": "changed"},
        {"state": "SUCCEEDED"},
        {"event_id": "new", "revision": 3, "execution_id": "changed"},
        {
            "event_id": "new",
            "revision": 3,
            "state": "REJECTED",
            "execution_id": None,
            "executed": False,
        },
    ],
)
def test_conflicting_facts_cannot_mutate_snapshot_or_accounts(database, setup_task, changes):
    dispatcher, command, observation = running(database, setup_task)
    before = row(database, command["action_id"])
    available(database)
    claim = claim_action(database)
    with pytest.raises(RuleViolation) as error:
        dispatcher.observe(claim, replace(observation, **changes))
    assert error.value.code == "INVALID_BINDING"
    assert row(database, command["action_id"]) == before
    with database.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(t.inbox)) >= 1
        assert connection.scalar(sa.select(sa.func.count()).select_from(t.settlements)) == 1
    assert dispatcher.observe(claim, observation)


def test_old_observation_does_not_clear_unknown_or_failure_count(database, setup_task):
    dispatcher, command, observation = running(database, setup_task)
    before = row(database, command["action_id"])
    available(database)
    dispatcher.observe(claim_action(database), replace(observation, event_id="older", revision=1))
    assert row(database, command["action_id"]) == before


def test_stale_worker_cannot_restore_snapshot(database, setup_task):
    dispatcher, command, observation = running(database, setup_task)
    available(database)
    old = claim_action(database, "old")
    with database.engine.begin() as connection:
        connection.execute(
            t.outbox.update()
            .where(t.outbox.c.event_id == old["event_id"])
            .values(lease_until=sa.func.now())
        )
    new = claim_action(database, "new")
    before = row(database, command["action_id"])
    assert not dispatcher.observe(old, observation)
    assert row(database, command["action_id"]) == before
    assert dispatcher.observe(new, observation)


@pytest.mark.parametrize("mismatch", ["execution", "account", "state"])
def test_legacy_equal_revision_requires_consistent_surviving_facts(database, setup_task, mismatch):
    dispatcher, command, observation = running(database, setup_task)
    with database.engine.begin() as connection:
        connection.execute(t.actions.update().values(last_external_observation=None))
        if mismatch == "account":
            connection.execute(t.reservations.update().values(charged=False))
        elif mismatch == "state":
            connection.execute(t.actions.update().values(state="SUCCEEDED"))
    available(database)
    claim = claim_action(database)
    if mismatch == "execution":
        observation = replace(observation, event_id="changed", execution_id="another")
    with pytest.raises(RuleViolation):
        dispatcher.observe(claim, observation)
    assert row(database, command["action_id"])["last_external_observation"] is None
