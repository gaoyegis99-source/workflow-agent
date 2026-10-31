from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from platform_checkers.open_science import check as open_check
from platform_core.application.acceptance import AcceptanceProcessor
from platform_core.application.catalog import OPEN_CHECK_TARGET, build_catalog
from platform_core.application.checker_catalog import build_catalog as build_checker_catalog
from platform_core.application.control import ControlService, digest
from platform_core.application.lifecycle import KnowledgeLifecycleProcessor
from platform_core.application.research import Research
from platform_core.application.science import Science
from platform_core.domain.control import RuleViolation
from platform_infrastructure import database as platform_database
from platform_knowledge import database as knowledge_database_module
from platform_knowledge import tables as knowledge_tables
from platform_knowledge.api import create_app as create_knowledge_app
from platform_knowledge.blobs import Blobs


def migration_config(connection, module):
    value = Config()
    value.set_main_option("script_location", str(Path(module.__file__).parent / "migrations"))
    value.attributes["connection"] = connection
    return value


def open_spec():
    return {
        "profile": "open-science-v1",
        "delivery_class": "EXPLORATORY",
        "requirements": [
            {
                "requirement_id": "evidence-clause",
                "description": "Use exact evidence",
                "necessary": True,
                "checks": ["DATA_VALIDITY"],
            }
        ],
        "required_outputs": [],
        "allow_manual_review": False,
        "max_check_executions": 1,
    }


def test_p53_migrations_are_current_and_outbox_is_leased(database, knowledge_database):
    database.migrate()
    database.check_schema()
    knowledge_database.migrate()
    knowledge_database.check_schema()
    platform_tables = set(sa.inspect(database.engine).get_table_names())
    assert {
        "acceptance_coverage_plans",
        "acceptance_coverage_items",
        "completion_validity",
        "knowledge_lifecycle_cursors",
    } <= platform_tables
    columns = {
        value["name"]
        for value in sa.inspect(knowledge_database.engine).get_columns("knowledge_outbox")
    }
    assert {"done", "owner", "generation", "lease_until", "attempts"} <= columns


def test_0007_and_knowledge_0001_upgrade_preserve_existing_records(
    database, knowledge_database, owner
):
    actor, _ = owner
    project = ControlService(database).create_project(actor, "p53-upgrade", "Upgrade")
    with database.engine.begin() as connection:
        command.downgrade(migration_config(connection, platform_database), "0007_research_policy")
        assert connection.scalar(sa.text("SELECT count(*) FROM projects")) == 1
    database.migrate()
    database.migrate()
    database.check_schema()
    with database.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.text("SELECT name FROM projects WHERE project_id=:project"),
                {"project": project["project_id"]},
            )
            == "Upgrade"
        )

    with knowledge_database.engine.begin() as connection:
        command.downgrade(migration_config(connection, knowledge_database_module), "0001_knowledge")
        connection.execute(
            sa.text(
                "INSERT INTO knowledge_outbox(event_id,payload,created_at) "
                "VALUES ('old-event','{\"kind\":\"CREATED\"}'::jsonb,clock_timestamp())"
            )
        )
    knowledge_database.migrate()
    knowledge_database.migrate()
    knowledge_database.check_schema()
    with knowledge_database.engine.connect() as connection:
        row = (
            connection.execute(
                sa.text(
                    "SELECT payload,done,generation,attempts FROM knowledge_outbox "
                    "WHERE event_id='old-event'"
                )
            )
            .mappings()
            .one()
        )
        assert dict(row) == {
            "payload": {"kind": "CREATED"},
            "done": False,
            "generation": 0,
            "attempts": 0,
        }


def test_authenticated_lifecycle_claim_is_fenced_and_acknowledged(knowledge_database, tmp_path):
    with knowledge_database.engine.begin() as connection:
        connection.execute(
            knowledge_tables.outbox.insert().values(
                event_id="event",
                payload={
                    "event_id": "event",
                    "project_id": "project",
                    "document_id": "document",
                    "version_id": "1",
                    "revision": 2,
                    "kind": "WITHDRAWN",
                    "reason": "superseded",
                },
                created_at=datetime.now(UTC),
            )
        )
    token = "k" * 48
    with TestClient(
        create_knowledge_app(knowledge_database, Blobs(tmp_path / "knowledge"), {"platform": token})
    ) as client:
        headers = {"Authorization": "Bearer " + token}
        claim = client.post(
            "/internal/lifecycle-events/claim",
            headers=headers,
            json={"owner": "worker", "lease_seconds": 30},
        ).json()["event"]
        assert claim["payload"]["kind"] == "WITHDRAWN"
        stale = client.post(
            "/internal/lifecycle-events/event/ack",
            headers=headers,
            json={"owner": "worker", "generation": claim["generation"] + 1},
        )
        assert stale.status_code == 409
        assert (
            client.post(
                "/internal/lifecycle-events/event/renew",
                headers=headers,
                json={"owner": "worker", "generation": claim["generation"]},
            ).status_code
            == 200
        )
        assert (
            client.post(
                "/internal/lifecycle-events/event/ack",
                headers=headers,
                json={"owner": "worker", "generation": claim["generation"]},
            ).status_code
            == 200
        )
        assert client.post(
            "/internal/lifecycle-events/claim",
            headers=headers,
            json={"owner": "worker", "lease_seconds": 30},
        ).json() == {"event": None}


def test_withdrawal_marks_current_validity_stale_without_rewriting_report(database, owner):
    actor, _ = owner
    control = ControlService(database)
    project = control.create_project(actor, "p53-project", "P53")["project_id"]
    task = control.create_task(
        actor,
        project,
        "p53-task",
        {
            "execution_mode": "DIRECT",
            "spec": {
                "goal": "Evidence based result",
                "requirements": [],
                "acceptance_spec": open_spec(),
            },
            "budget": {"execution_count": 2, "max_in_flight": 1},
        },
    )
    control.grant(
        actor,
        project,
        "p53-grant",
        {
            "task_id": task["task_id"],
            "targets": [OPEN_CHECK_TARGET],
            "resources": [],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "derived_outputs": True,
            "acceptance_profiles": ["open-science-v1"],
        },
    )
    evidence_ref = {
        "project_id": project,
        "document_id": "document",
        "version_id": "1",
        "unit_id": "unit",
        "content_digest": "sha256:" + "a" * 64,
        "start_char": 0,
        "end_char": 4,
        "exact_text_digest": "sha256:" + "b" * 64,
        "normalization": "utf8-nfc-lf-v1",
    }
    artifact = {
        "ref": {
            "project_id": project,
            "asset_id": "asset",
            "version_id": "1",
            "version_manifest_digest": "sha256:" + "c" * 64,
        },
        "manifest": {
            "representations": [{"representation_id": "result", "digest": "sha256:" + "d" * 64}]
        },
    }
    with database.transaction() as uow:
        uow.science.add_candidate(
            {
                "deliverable_id": "deliverable",
                "project_id": project,
                "task_id": task["task_id"],
                "spec_revision": 1,
                "digest": digest({"artifact": artifact}),
                "payload": {
                    "artifacts": [artifact],
                    "executions": ["execution"],
                    "workflow_outputs": [],
                },
            }
        )
        uow.research.add_provenance(
            {
                "relation_id": "relation",
                "project_id": project,
                "task_id": task["task_id"],
                "relation": "CITED",
                "provenance_kind": "OBSERVED_PROVENANCE",
                "subject": {"kind": "CLAIM_REVISION", "object_id": "claim", "revision": 1},
                "object": {"kind": "EVIDENCE", "ref": evidence_ref},
                "activity_execution_id": None,
                "agent_id": actor.principal_id,
                "role": "basis",
                "recorded_at": uow.now(),
                "relation_digest": "sha256:" + "e" * 64,
            }
        )
    receipt = Science(
        database,
        None,
        None,
        operator_catalog=build_catalog(),
        checker_catalog=build_checker_catalog(),
    ).accept(
        actor,
        project,
        task["task_id"],
        "p53-accept",
        {
            "expected_spec_revision": 1,
            "deliverable_id": "deliverable",
            "retry_of": None,
        },
    )
    with database.transaction() as uow:
        record = uow.science.check(project, receipt["acceptance_id"])
        report = {"profile": "open-science-v1", "status": "PASS", "historical": True}
        uow.science.update_check(record["acceptance_id"], "PASS", report, digest(report))
        current = uow.tasks.get(project, task["task_id"], lock=True)
        uow.actions.update(record["action_id"], stopped=True, state="SUCCEEDED")
        uow.science.complete(current, record | {"report_digest": digest(report)})
        uow.science.set_completion_validity(task["task_id"], "CURRENT")
        uow.tasks.save(
            replace(current, lifecycle="COMPLETED", control_version=current.control_version + 1)
        )
    event = {
        "event_id": "withdrawal",
        "project_id": project,
        "document_id": "document",
        "version_id": "1",
        "revision": 3,
        "kind": "WITHDRAWN",
        "reason": "superseded",
    }
    with database.transaction() as uow:
        changed = uow.science.apply_knowledge_event(event)
        assert len(changed) == 1
        assert uow.science.validity(task["task_id"])["validity"] == "STALE"
        assert uow.science.check(project, receipt["acceptance_id"])["report"]["historical"] is True
        assert uow.tasks.get(project, task["task_id"]).lifecycle == "COMPLETED"
        assert uow.science.plan(receipt["acceptance_id"])["status"] == "STALE"
        assert uow.science.apply_knowledge_event(event) == []
        assert uow.science.apply_knowledge_event(event | {"event_id": "older", "revision": 2}) == []
    task_view = control.task(actor, project, task["task_id"])
    assert task_view["completion_validity"] == "STALE"
    assert task_view["completion_validity_revision"] == 2


def test_lifecycle_response_loss_replays_through_inbox_without_duplicate_effect(database):
    event = {
        "event_id": "lifecycle-event",
        "project_id": "project",
        "document_id": "document",
        "version_id": "1",
        "revision": 1,
        "kind": "CREATED",
    }

    class Knowledge:
        def __init__(self):
            self.claims = 0

        def json(self, method, path, json):
            if path.endswith("/claim"):
                self.claims += 1
                return {
                    "event": {
                        "event_id": event["event_id"],
                        "generation": self.claims,
                        "payload": event,
                    }
                }
            if path.endswith("/renew"):
                return {"status": "RENEWED"}
            if self.claims == 1:
                raise RuleViolation("DEPENDENCY_UNAVAILABLE", "ack lost", 503)
            return {"status": "ACKNOWLEDGED"}

    knowledge = Knowledge()
    processor = KnowledgeLifecycleProcessor(database, knowledge, "worker")
    try:
        processor.process_one()
    except RuleViolation as error:
        assert error.code == "DEPENDENCY_UNAVAILABLE"
    assert processor.process_one() is True
    with database.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.text(
                    "SELECT count(*) FROM inbox WHERE source='KNOWLEDGE' "
                    "AND event_id='lifecycle-event'"
                )
            )
            == 1
        )


def test_historical_imported_event_is_consumed_before_withdrawals(database):
    event = {
        "event_id": "historical-import",
        "project_id": "project",
        "document_id": "document",
        "version_id": "1",
        "revision": 1,
        "kind": "IMPORTED",
    }

    class Knowledge:
        def __init__(self):
            self.acked = False

        def json(self, method, path, json):
            if path.endswith("/claim"):
                return {
                    "event": None
                    if self.acked
                    else {"event_id": event["event_id"], "generation": 1, "payload": event}
                }
            if path.endswith("/renew"):
                return {"status": "RENEWED"}
            self.acked = True
            return {"status": "ACKNOWLEDGED"}

    knowledge = Knowledge()
    processor = KnowledgeLifecycleProcessor(database, knowledge, "worker")
    assert processor.process_one() is True
    assert knowledge.acked is True
    assert processor.process_one() is False


def test_open_checker_fact_completes_exploratory_delivery(database, owner):
    actor, _ = owner
    control = ControlService(database)
    project = control.create_project(actor, "p53-complete-project", "P53")["project_id"]
    task = control.create_task(
        actor,
        project,
        "p53-complete-task",
        {
            "execution_mode": "DIRECT",
            "spec": {
                "goal": "Exploratory delivery",
                "requirements": [],
                "acceptance_spec": open_spec(),
            },
            "budget": {"execution_count": 1, "max_in_flight": 1},
        },
    )
    control.grant(
        actor,
        project,
        "p53-complete-grant",
        {
            "task_id": task["task_id"],
            "targets": [OPEN_CHECK_TARGET],
            "resources": [],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "derived_outputs": True,
            "acceptance_profiles": ["open-science-v1"],
        },
    )
    artifact = {
        "ref": {
            "project_id": project,
            "asset_id": "asset",
            "version_id": "1",
            "version_manifest_digest": "sha256:" + "1" * 64,
        },
        "manifest": {
            "representations": [{"representation_id": "result", "digest": "sha256:" + "2" * 64}]
        },
    }
    with database.transaction() as uow:
        uow.science.add_candidate(
            {
                "deliverable_id": "deliverable",
                "project_id": project,
                "task_id": task["task_id"],
                "spec_revision": 1,
                "digest": digest(artifact),
                "payload": {
                    "artifacts": [artifact],
                    "executions": ["execution"],
                    "workflow_outputs": [],
                },
            }
        )
    receipt = Science(
        database,
        None,
        None,
        operator_catalog=build_catalog(),
        checker_catalog=build_checker_catalog(),
    ).accept(
        actor,
        project,
        task["task_id"],
        "p53-complete",
        {
            "expected_spec_revision": 1,
            "deliverable_id": "deliverable",
            "retry_of": None,
        },
    )
    with database.transaction() as uow:
        accepted = uow.science.check(project, receipt["acceptance_id"])
        action = uow.actions.get(accepted["action_id"])
        uow.actions.update(
            action["action_id"], stopped=True, state="SUCCEEDED", execution_id="checker"
        )
        frozen = action["request"]

    class Execution:
        def json(self, method, path):
            assert method == "GET" and path.endswith("/checker")
            return {
                "action_id": accepted["action_id"],
                "request": frozen,
                "state": "SUCCEEDED",
                "executed": True,
                "runtime": {"check_result": open_check(frozen)},
            }

    assert AcceptanceProcessor(database, Execution()).process() is True
    view = Science(
        database,
        None,
        None,
        operator_catalog=build_catalog(),
        checker_catalog=build_checker_catalog(),
    ).checks(actor, project, task["task_id"], receipt["acceptance_id"])[0]
    assert view["status"] == "PASS"
    assert view["report"]["delivery_class"] == "EXPLORATORY"
    assert view["completion"]["validity"] == "CURRENT"


def test_research_change_after_check_submission_makes_coverage_stale(database, owner):
    actor, _ = owner
    control = ControlService(database)
    project = control.create_project(actor, "p53-fresh-project", "P53")["project_id"]
    task = control.create_task(
        actor,
        project,
        "p53-fresh-task",
        {
            "execution_mode": "DIRECT",
            "spec": {
                "goal": "Frozen coverage",
                "requirements": [],
                "acceptance_spec": open_spec(),
            },
            "budget": {"execution_count": 1, "max_in_flight": 1},
        },
    )
    control.grant(
        actor,
        project,
        "p53-fresh-grant",
        {
            "task_id": task["task_id"],
            "targets": [OPEN_CHECK_TARGET],
            "resources": [],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "derived_outputs": True,
            "acceptance_profiles": ["open-science-v1"],
        },
    )
    artifact = {
        "ref": {
            "project_id": project,
            "asset_id": "asset",
            "version_id": "1",
            "version_manifest_digest": "sha256:" + "6" * 64,
        },
        "manifest": {
            "representations": [{"representation_id": "result", "digest": "sha256:" + "7" * 64}]
        },
    }
    with database.transaction() as uow:
        uow.science.add_candidate(
            {
                "deliverable_id": "fresh-deliverable",
                "project_id": project,
                "task_id": task["task_id"],
                "spec_revision": 1,
                "digest": digest(artifact),
                "payload": {
                    "artifacts": [artifact],
                    "executions": ["execution"],
                    "workflow_outputs": [],
                },
            }
        )
    science = Science(
        database,
        None,
        None,
        operator_catalog=build_catalog(),
        checker_catalog=build_checker_catalog(),
    )
    receipt = science.accept(
        actor,
        project,
        task["task_id"],
        "p53-fresh-accept",
        {
            "expected_spec_revision": 1,
            "deliverable_id": "fresh-deliverable",
            "retry_of": None,
        },
    )
    # A new research revision changes the facts after the checker request was frozen.
    Research(database).create_analysis(
        actor,
        project,
        task["task_id"],
        "p53-late-analysis",
        {
            "expected_spec_revision": 1,
            "supersedes_revision_id": None,
            "revision_reason": None,
            "question": "A late exploratory question",
            "research_objects": ["result"],
            "spatial_scope": None,
            "temporal_scope": None,
            "input_roles": [],
            "methods": [],
            "primary_metrics": [],
            "comparison_rules": [],
            "data_splits": [],
            "stopping_rules": [],
            "allowed_deviations": [],
            "checker_versions": [],
            "observed_sources": [],
            "groundwater_comparison": None,
        },
        [],
    )
    with database.transaction() as uow:
        accepted = uow.science.check(project, receipt["acceptance_id"])
        action = uow.actions.get(accepted["action_id"])
        uow.actions.update(
            action["action_id"], stopped=True, state="SUCCEEDED", execution_id="checker-fresh"
        )
        historical = {
            "profile": "open-science-v1",
            "status": "PASS",
            "plan_digest": action["request"]["coverage_plan"]["plan_digest"],
            "items": [],
        }
        uow.science.update_check(accepted["acceptance_id"], "PASS", historical, digest(historical))

    class Execution:
        def json(self, method, path):
            raise AssertionError("A stored historical checker report must not be fetched again")

    assert AcceptanceProcessor(database, Execution()).process() is True
    view = science.checks(actor, project, task["task_id"], receipt["acceptance_id"])[0]
    assert {"owner", "generation", "lease_until", "attempts"}.isdisjoint(view)
    assert view["status"] == "STALE"
    assert view["coverage_plan"]["status"] == "STALE"
    assert view["completion"] is None


def test_verified_human_review_completes_only_after_owner_decision(database, owner):
    actor, _ = owner
    control = ControlService(database)
    acceptance_spec = {
        "profile": "open-science-v1",
        "delivery_class": "VERIFIED",
        "requirements": [
            {
                "requirement_id": "expert-interpretation",
                "description": "Expert interpretation",
                "necessary": True,
                "checks": ["HUMAN_REVIEW"],
            }
        ],
        "required_outputs": [
            {
                "role_id": "review_result",
                "necessary": True,
                "expected": {
                    "result_kind": "OPERATOR_RESULT",
                    "operator_id": "statistics-linear-trend",
                    "operator_version": "1.0.0",
                    "output_port": "result",
                    "schema_id": "p3-result-v1",
                },
            }
        ],
        "allow_manual_review": True,
        "max_check_executions": 1,
    }
    project = control.create_project(actor, "p53-review-project", "P53")["project_id"]
    task = control.create_task(
        actor,
        project,
        "p53-review-task",
        {
            "execution_mode": "DIRECT",
            "spec": {
                "goal": "Verified review",
                "requirements": [],
                "acceptance_spec": acceptance_spec,
            },
            "budget": {"execution_count": 1, "max_in_flight": 1},
        },
    )
    research = Research(database)
    analysis = research.create_analysis(
        actor,
        project,
        task["task_id"],
        "analysis",
        {
            "expected_spec_revision": 1,
            "supersedes_revision_id": None,
            "revision_reason": None,
            "question": "Is the interpretation acceptable?",
            "research_objects": ["result"],
            "spatial_scope": None,
            "temporal_scope": None,
            "input_roles": [],
            "methods": [],
            "primary_metrics": [],
            "comparison_rules": [],
            "data_splits": [],
            "stopping_rules": [],
            "allowed_deviations": [],
            "checker_versions": [],
            "observed_sources": [],
            "groundwater_comparison": None,
        },
        [],
    )
    registration = research.register(
        actor,
        project,
        task["task_id"],
        analysis["revision_id"],
        "register",
        {"expected_spec_revision": 1},
    )
    control.grant(
        actor,
        project,
        "p53-review-grant",
        {
            "task_id": task["task_id"],
            "targets": [OPEN_CHECK_TARGET],
            "resources": [],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            "derived_outputs": True,
            "acceptance_profiles": ["open-science-v1"],
        },
    )
    artifact = {
        "ref": {
            "project_id": project,
            "asset_id": "asset",
            "version_id": "1",
            "version_manifest_digest": "sha256:" + "4" * 64,
        },
        "manifest": {
            "representations": [
                {
                    "representation_id": "result",
                    "digest": "sha256:" + "5" * 64,
                    "schema_id": "p3-result-v1",
                }
            ]
        },
    }
    output_ref = {
        "kind": "OPERATOR_OUTPUT",
        "action_id": "result-action",
        "execution_id": "execution",
        "output_port": "result",
        "asset_ref": artifact["ref"],
        "representation_id": "result",
        "representation_digest": "sha256:" + "5" * 64,
        "schema_id": "p3-result-v1",
        "schema_source": "MANIFEST",
    }
    with database.transaction() as uow:
        uow.science.add_candidate(
            {
                "deliverable_id": "deliverable",
                "project_id": project,
                "task_id": task["task_id"],
                "spec_revision": 1,
                "digest": digest(artifact),
                "payload": {
                    "artifacts": [artifact],
                    "executions": ["execution"],
                    "workflow_outputs": [],
                    "role_bindings": [{"role_id": "review_result", "output_ref": output_ref}],
                    "identity_mode": "EXACT",
                },
                "identity_mode": "EXACT",
            }
        )
    science = Science(
        database,
        None,
        None,
        operator_catalog=build_catalog(),
        checker_catalog=build_checker_catalog(),
    )
    receipt = science.accept(
        actor,
        project,
        task["task_id"],
        "accept",
        {
            "expected_spec_revision": 1,
            "deliverable_id": "deliverable",
            "retry_of": None,
            "analysis_registration_id": registration["registration_id"],
        },
    )
    with database.transaction() as uow:
        accepted = uow.science.check(project, receipt["acceptance_id"])
        action = uow.actions.get(accepted["action_id"])
        uow.actions.update(
            action["action_id"], stopped=True, state="SUCCEEDED", execution_id="checker"
        )
        frozen = action["request"]

    class Execution:
        def json(self, method, path):
            return {
                "action_id": accepted["action_id"],
                "request": frozen,
                "state": "SUCCEEDED",
                "executed": True,
                "runtime": {"check_result": open_check(frozen)},
            }

    AcceptanceProcessor(database, Execution()).process()
    pending = science.checks(actor, project, task["task_id"], receipt["acceptance_id"])[0]
    assert pending["status"] == "REVIEW_REQUIRED"
    assert pending["completion"] is None
    item = next(
        value
        for value in pending["coverage_plan"]["items"]
        if value["check_type"] == "HUMAN_REVIEW"
    )
    editor_id = database.create_principal("review-editor")
    editor_token = database.issue_token(editor_id)["token"]
    database.set_member(project, editor_id, "EDITOR", actor=actor)
    editor = database.authenticate(editor_token)
    with pytest.raises(RuleViolation) as denied:
        science.review(
            editor,
            project,
            task["task_id"],
            receipt["acceptance_id"],
            "editor-review",
            {
                "expected_spec_revision": 1,
                "coverage_plan_revision": 1,
                "item_id": item["item_id"],
                "qualification": "Reviewer",
                "criteria": "Declared scope",
                "decision": "PASS",
                "reason": "Attempted decision",
            },
        )
    assert denied.value.code == "SCOPE_DENIED"
    review = science.review(
        actor,
        project,
        task["task_id"],
        receipt["acceptance_id"],
        "review",
        {
            "expected_spec_revision": 1,
            "coverage_plan_revision": 1,
            "item_id": item["item_id"],
            "qualification": "Hydrogeologist",
            "criteria": "Declared scope and limitations",
            "decision": "PASS",
            "reason": "Interpretation is supported within the stated scope.",
        },
    )
    assert review["acceptance_status"] == "PASS"
    finished = science.checks(actor, project, task["task_id"], receipt["acceptance_id"])[0]
    assert finished["completion"]["validity"] == "CURRENT"
