"""Test fixture for real 0001 data; also run against separately installed wheels."""

import argparse
import hashlib
import json
import os
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from platform_core.application.control import ControlService, digest
from platform_core.application.dispatch import Dispatcher
from platform_core.domain.control import ExternalObservation
from platform_infrastructure import database as database_module
from platform_infrastructure.database import Database
from platform_infrastructure.migrations.schema_0001 import metadata


def migrate_to(database, revision, *, downgrade=False):
    cfg = Config()
    cfg.set_main_option(
        "script_location", str(Path(database_module.__file__).parent / "migrations")
    )
    with database.engine.begin() as connection:
        cfg.attributes["connection"] = connection
        (command.downgrade if downgrade else command.upgrade)(cfg, revision)


def fingerprint(database):
    # Compare every original column, including credentials' digests, grants,
    # leases, idempotency results and append-only settlement history.
    with database.engine.connect() as connection:
        rows = {
            table.name: [
                dict(row)
                for row in connection.execute(
                    sa.select(table).order_by(*table.primary_key.columns)
                ).mappings()
            ]
            for table in metadata.sorted_tables
        }
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()


def seed(database):
    app = ControlService(database)
    principal = database.create_principal("legacy-owner")
    raw = database.issue_token(principal)["token"]
    actor = database.authenticate(raw)
    project = app.create_project(actor, "legacy-project", "Upgrade fixture")["project_id"]
    task = app.create_task(
        actor,
        project,
        "legacy-task",
        {
            "execution_mode": "DIRECT",
            "spec": {"goal": "Upgrade with recoverable actions", "requirements": []},
            "budget": {"execution_count": 5, "max_in_flight": 3},
        },
    )["task_id"]
    target = {
        "kind": "OPERATOR",
        "operator_id": "p2-fixture",
        "version_id": "1",
        "execution_binding": {"kind": "LOCAL", "implementation_id": "test-peer"},
    }
    with database.transaction() as uow:
        expiry = (uow.now() + timedelta(hours=1)).isoformat()
    app.grant(
        actor,
        project,
        "legacy-grant",
        {"task_id": task, "targets": [target], "resources": [], "expires_at": expiry},
    )
    request = {
        "task_id": task,
        "expected_spec_revision": 1,
        "target": target,
        "resources": [],
        "parameters": {},
    }
    # Write the three P2 actions through the frozen 0001 schema.  Current
    # repositories intentionally require the current migration and therefore
    # must never run against this old database during an upgrade fixture.
    commands = []
    t = metadata.tables
    with database.engine.begin() as c:
        grant_epoch = c.scalar(sa.select(t["tasks"].c.grant_epoch))
        for key in ["queued", "unknown", "known"]:
            action_id, command_id = uuid4().hex, uuid4().hex
            command_record = {
                "command_id": command_id,
                "project_id": project,
                "task_id": task,
                "spec_revision": 1,
                "status": "RECORDED",
                "action_id": action_id,
                "execution_id": None,
                "error_code": None,
                "error_message": None,
            }
            c.execute(t["commands"].insert().values(**command_record))
            c.execute(
                t["actions"]
                .insert()
                .values(
                    action_id=action_id,
                    command_id=command_id,
                    task_id=task,
                    project_id=project,
                    spec_revision=1,
                    grant_epoch=grant_epoch,
                    principal_id=actor.principal_id,
                    token_id=actor.token_id,
                    frame_id=uuid4().hex,
                    decision_source="CLIENT",
                    request=request,
                    send_started=False,
                    state="RECORDED",
                    external_revision=0,
                    execution_id=None,
                    stopped=False,
                    cancel_requested=False,
                    failure_count=0,
                )
            )
            c.execute(
                t["reservations"]
                .insert()
                .values(action_id=action_id, task_id=task, charged=False, stopped=False)
            )
            c.execute(
                t["outbox"]
                .insert()
                .values(
                    event_id=uuid4().hex,
                    task_id=task,
                    kind="ACTION",
                    payload={"action_id": action_id, "project_id": project},
                )
            )
            c.execute(
                t["idempotency"]
                .insert()
                .values(
                    principal_id=actor.principal_id,
                    scope=project,
                    operation="submit_action",
                    key=key,
                    digest=digest(request),
                    result=command_record,
                )
            )
            commands.append(command_record)
        c.execute(t["budgets"].update().values(reserved=3, in_flight=3))
    queued, unknown, known = commands
    observation = ExternalObservation(
        "legacy:1", known["action_id"], 1, "RUNNING", "legacy-execution", True
    )
    with database.engine.begin() as c:
        c.execute(
            t["actions"]
            .update()
            .where(t["actions"].c.action_id.in_([unknown["action_id"], known["action_id"]]))
            .values(send_started=True, state="RECONCILING", failure_count=2)
        )
        c.execute(
            t["actions"]
            .update()
            .where(t["actions"].c.action_id == known["action_id"])
            .values(external_revision=1, execution_id="legacy-execution")
        )
        c.execute(
            t["commands"]
            .update()
            .where(t["commands"].c.command_id == known["command_id"])
            .values(status="RESOLVED", execution_id="legacy-execution")
        )
        c.execute(
            t["commands"]
            .update()
            .where(t["commands"].c.command_id == unknown["command_id"])
            .values(status="RECONCILING")
        )
        c.execute(
            t["reservations"]
            .update()
            .where(t["reservations"].c.action_id == known["action_id"])
            .values(charged=True)
        )
        c.execute(t["budgets"].update().values(used=1, reserved=2))
        c.execute(
            t["budget_settlements"]
            .insert()
            .values(
                action_id=known["action_id"],
                source="EXECUTION",
                event_id=observation.event_id,
                charged=True,
                stopped=False,
            )
        )
        c.execute(
            t["inbox"]
            .insert()
            .values(
                source="EXECUTION",
                event_id=observation.event_id,
                digest=digest(asdict(observation)),
            )
        )
        c.execute(
            t["outbox"]
            .update()
            .values(
                owner="old-worker",
                generation=7,
                attempts=9,
                lease_until=sa.func.now() - timedelta(seconds=1),
            )
        )
    return {
        "token": raw,
        "project": project,
        "task": task,
        "commands": commands,
        "observation": asdict(observation),
        "fingerprint": fingerprint(database),
    }


def verify(database, fixture):
    database.check_schema()
    assert fingerprint(database) == fixture["fingerprint"]
    with database.engine.connect() as c:
        assert (
            c.scalar(
                sa.text("SELECT count(*) FROM actions WHERE last_external_observation IS NOT NULL")
            )
            == 0
        )
    app = ControlService(database)
    actor = database.authenticate(fixture["token"])
    for command_record in fixture["commands"]:
        assert (
            app.command(actor, fixture["project"], command_record["command_id"])["action_id"]
            == command_record["action_id"]
        )


def recover_known(database, fixture):
    dispatcher = Dispatcher(database)
    known = fixture["commands"][2]
    with database.engine.begin() as c:
        c.execute(sa.text("UPDATE outbox SET available_at=clock_timestamp()+interval '1 hour'"))
        c.execute(
            sa.text(
                "UPDATE outbox SET available_at=clock_timestamp() "
                "WHERE payload->>'action_id'=:action"
            ),
            {"action": known["action_id"]},
        )
    claim = dispatcher.claim("new-worker")
    assert claim["generation"] == 8
    assert dispatcher.observe(claim, ExternalObservation(**fixture["observation"]))
    with database.engine.connect() as c:
        action = (
            c.execute(
                sa.text("SELECT * FROM actions WHERE action_id=:action"),
                {"action": known["action_id"]},
            )
            .mappings()
            .one()
        )
        assert action["state"] == "RUNNING" and action["failure_count"] == 0
        assert action["last_external_observation"]["execution_id"] == "legacy-execution"
        assert c.scalar(sa.text("SELECT count(*) FROM budget_settlements")) == 1
        assert tuple(c.execute(sa.text("SELECT used, reserved, in_flight FROM budgets")).one()) == (
            1,
            2,
            3,
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["seed", "verify"])
    parser.add_argument("fixture", type=Path)
    args = parser.parse_args()
    database = Database(os.environ["V7_DATABASE_URL"])
    try:
        if args.mode == "seed":
            migrate_to(database, "0001_control")
            args.fixture.write_text(json.dumps(seed(database)))
        else:
            fixture = json.loads(args.fixture.read_text())
            verify(database, fixture)
            recover_known(database, fixture)
    finally:
        database.close()


if __name__ == "__main__":
    main()
