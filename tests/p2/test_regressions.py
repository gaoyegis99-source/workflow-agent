"""Regression cases captured before the P2 follow-up fixes."""

from threading import Event, Thread, current_thread

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from platform_api.app import create_app
from platform_core.application.dispatch import Dispatcher
from platform_core.domain.control import ExternalObservation
from platform_infrastructure import tables as t

from .conftest import TASK
from .test_actions import action_request, available, claim_action


@pytest.mark.parametrize("operation", ["create", "pause", "action"])
@pytest.mark.parametrize("role", ["VIEWER", "REMOVE"])
def test_membership_change_does_not_deadlock_business_write(database, setup_task, operation, role):
    app, owner, _, project, task, _ = setup_task
    editor_id = database.create_principal("editor")
    editor = database.authenticate(database.issue_token(editor_id)["token"])
    database.set_member(project, editor_id, "EDITOR", actor=owner)
    reached_write, reached_member_lock = Event(), Event()
    outcomes = {}
    gate = "INSERT INTO tasks" if operation == "create" else "INSERT INTO commands"

    def synchronize(conn, cursor, statement, parameters, context, executemany):
        if current_thread().name == "business" and statement.startswith(gate):
            reached_write.set()
            assert reached_member_lock.wait(10)
        if (
            current_thread().name == "membership"
            and "FROM members" in statement
            and "FOR UPDATE" in statement
        ):
            reached_member_lock.set()

    def business():
        if operation == "create":
            return app.create_task(editor, project, "race", TASK)
        if operation == "pause":
            return app.control(editor, project, task, "pause", "race", 1)
        return app.submit_action(editor, project, "race", action_request(task))

    def run(name, function):
        try:
            function()
            outcomes[name] = "completed"
        except Exception as error:
            outcomes[name] = getattr(getattr(error, "orig", None), "sqlstate", type(error).__name__)

    sa.event.listen(database.engine, "before_cursor_execute", synchronize)
    first = Thread(name="business", target=run, args=("business", business))
    second = Thread(
        name="membership",
        target=run,
        args=("membership", lambda: database.set_member(project, editor_id, role, actor=owner)),
    )
    try:
        first.start()
        assert reached_write.wait(10)
        second.start()
        first.join(15)
        second.join(15)
        assert not first.is_alive() and not second.is_alive()
        assert outcomes == {"business": "completed", "membership": "completed"}
    finally:
        reached_member_lock.set()
        first.join(15)
        if second.ident is not None:
            second.join(15)
        sa.event.remove(database.engine, "before_cursor_execute", synchronize)


def test_identical_observation_recovers_after_communication_failure(database, setup_task):
    app, actor, _, project, task, _ = setup_task
    command = app.submit_action(actor, project, "recover", action_request(task))
    dispatcher = Dispatcher(database)
    claim = claim_action(database)
    dispatcher.prepare(claim)
    observation = ExternalObservation(
        "running:1", command["action_id"], 1, "RUNNING", "peer-1", True
    )
    dispatcher.observe(claim, observation)
    available(database)
    dispatcher.uncertain(claim_action(database))
    available(database)
    dispatcher.observe(claim_action(database), observation)
    with database.engine.connect() as connection:
        action = connection.execute(sa.select(t.actions)).mappings().one()
        assert action["state"] == "RUNNING"
        assert action["failure_count"] == 0
        assert connection.scalar(sa.select(sa.func.count()).select_from(t.settlements)) == 1
    budget = app.budget(actor, project, task)
    assert (budget["used"], budget["reserved"], budget["in_flight"]) == (1, 0, 1)


def test_historical_invalid_binding_returns_structured_conflict(database, setup_task):
    app, actor, token, project, task, _ = setup_task
    command = app.submit_action(actor, project, "bad-binding", action_request(task))
    with database.engine.begin() as connection:
        connection.execute(
            t.commands.update()
            .where(t.commands.c.command_id == command["command_id"])
            .values(status="RESOLVED", execution_id="peer/invalid")
        )
    with TestClient(create_app(database), raise_server_exceptions=False) as client:
        response = client.get(
            f"/api/v1/projects/{project}/commands/{command['command_id']}",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert response.status_code == 409
    assert response.json()["code"] == "INVALID_BINDING"


@pytest.mark.parametrize("operation", ["create", "pause", "action"])
@pytest.mark.parametrize("role", ["VIEWER", "REMOVE"])
def test_membership_change_committed_first_denies_write(database, setup_task, operation, role):
    from platform_core.domain.control import RuleViolation

    app, owner, _, project, task, _ = setup_task
    principal = database.create_principal("editor")
    editor = database.authenticate(database.issue_token(principal)["token"])
    database.set_member(project, principal, "EDITOR", actor=owner)
    database.set_member(project, principal, role, actor=owner)
    with pytest.raises(RuleViolation) as error:
        if operation == "create":
            app.create_task(editor, project, "denied", TASK)
        elif operation == "pause":
            app.control(editor, project, task, "pause", "denied", 1)
        else:
            app.submit_action(editor, project, "denied", action_request(task))
    assert error.value.status == (403 if role == "VIEWER" else 404)
    assert app.task(owner, project, task)["control_version"] == 1
    assert app.budget(owner, project, task)["reserved"] == 0


def test_concurrent_owner_removals_preserve_last_owner(database, setup_task):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from platform_core.domain.control import RuleViolation

    _, owner, _, project, _, _ = setup_task
    principal = database.create_principal("second-owner")
    other = database.authenticate(database.issue_token(principal)["token"])
    database.set_member(project, principal, "OWNER", actor=owner)
    barrier = Barrier(2)

    def remove(actor):
        barrier.wait()
        try:
            database.set_member(project, actor.principal_id, "REMOVE", actor=actor)
            return "removed"
        except RuleViolation as error:
            return error.code

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(remove, [owner, other]))
    assert sorted(results) == ["PRECONDITION_UNRESOLVED", "removed"]
    with database.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(sa.func.count()).select_from(t.members).where(t.members.c.role == "OWNER")
            )
            == 1
        )


def test_illegal_external_identity_never_settles(database, setup_task):
    from platform_core.domain.control import RuleViolation

    app, actor, _, project, task, _ = setup_task
    command = app.submit_action(actor, project, "illegal", action_request(task))
    dispatcher = Dispatcher(database)
    claim = claim_action(database)
    dispatcher.prepare(claim)
    with pytest.raises(RuleViolation, match="identity"):
        dispatcher.observe(
            claim,
            ExternalObservation(
                "event:1", command["action_id"], 1, "RUNNING", "peer/invalid", True
            ),
        )
    assert app.budget(actor, project, task)["reserved"] == 1
    with database.engine.connect() as connection:
        assert connection.scalar(sa.select(sa.func.count()).select_from(t.settlements)) == 0
