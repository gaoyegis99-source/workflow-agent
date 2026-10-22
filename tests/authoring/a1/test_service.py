import concurrent.futures

import pytest
import sqlalchemy as sa
from platform_core.application.authoring import AuthoringService
from platform_core.application.control import ControlService
from platform_core.application.errors import RuleViolation
from platform_infrastructure import tables

CREATE = {
    "expected_revision": 0,
    "stable_key": "draft-one",
    "metadata": {"title": "Draft one", "description": "", "labels": []},
    "layout": {},
}


def setup(database, owner):
    actor, token = owner
    project = ControlService(database).create_project(actor, "authoring-project", "Authoring")
    return AuthoringService(database), actor, token, project["project_id"]


def test_create_is_atomic_idempotent_and_has_stable_blank_source(database, owner):
    service, actor, _, project = setup(database, owner)
    first = service.create(actor, project, "create-one", CREATE)
    second = service.create(actor, project, "create-one", CREATE)
    assert second == first
    assert first["head_revision"] == 1
    assert first["head"]["source"] == {
        "cwlVersion": "v1.2",
        "class": "Workflow",
        "inputs": {},
        "outputs": {},
        "steps": {},
    }
    with database.engine.connect() as connection:
        definitions = connection.scalar(
            sa.select(sa.func.count()).select_from(tables.workflow_definitions)
        )
        revisions = connection.scalar(
            sa.select(sa.func.count()).select_from(tables.workflow_revisions)
        )
        assert definitions == revisions == 1


def test_append_copies_source_and_splits_layout_digest(database, owner):
    service, actor, _, project = setup(database, owner)
    definition = service.create(actor, project, "create", CREATE)
    request = {
        "expected_revision": 1,
        "source_digest": definition["head"]["source_digest"],
        "metadata": {"title": "Renamed", "description": "notes", "labels": []},
        "layout": {"viewport": {"x": 10}},
    }
    revision = service.append(actor, project, definition["definition_id"], "save", request)
    assert revision["revision"] == 2
    assert revision["source"] == definition["head"]["source"]
    assert revision["source_digest"] == definition["head"]["source_digest"]
    assert revision["layout_digest"] != definition["head"]["layout_digest"]
    assert service.append(actor, project, definition["definition_id"], "save", request) == revision


def test_create_and_append_key_spaces_do_not_alias(database, owner):
    service, actor, _, project = setup(database, owner)
    definition = service.create(actor, project, "same-key", CREATE)
    revision = service.append(
        actor,
        project,
        definition["definition_id"],
        "same-key",
        {
            "expected_revision": 1,
            "source_digest": definition["head"]["source_digest"],
            "metadata": {"title": "Second", "description": "", "labels": []},
            "layout": {},
        },
    )
    assert revision["revision"] == 2


def test_same_key_different_body_and_cross_project_are_rejected(database, owner):
    service, actor, _, project = setup(database, owner)
    created = service.create(actor, project, "create", CREATE)
    with pytest.raises(RuleViolation) as conflict:
        service.create(actor, project, "create", CREATE | {"stable_key": "other"})
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"

    second = ControlService(database).create_project(actor, "second-project", "Second")
    with pytest.raises(RuleViolation):
        service.definition(actor, second["project_id"], created["definition_id"])


def test_concurrent_revisions_only_one_wins(database, owner):
    service, actor, _, project = setup(database, owner)
    definition = service.create(actor, project, "create", CREATE)

    def save(key):
        return service.append(
            actor,
            project,
            definition["definition_id"],
            key,
            {
                "expected_revision": 1,
                "source_digest": definition["head"]["source_digest"],
                "metadata": {"title": key, "description": "", "labels": []},
                "layout": {},
            },
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(save, key) for key in ("left", "right")]
    outcomes = []
    for future in futures:
        try:
            outcomes.append(future.result())
        except RuleViolation as error:
            outcomes.append(error)
    assert sum(isinstance(item, dict) for item in outcomes) == 1
    assert any(
        isinstance(item, RuleViolation) and item.code == "REVISION_CONFLICT" for item in outcomes
    )


def test_replay_rechecks_membership(database, owner):
    service, actor, _, project = setup(database, owner)
    service.create(actor, project, "create", CREATE)
    other = database.create_principal("other")
    database.set_member(project, other, "OWNER", actor=actor)
    other_actor = database.authenticate(database.issue_token(other)["token"])
    database.set_member(project, actor.principal_id, "REMOVE", actor=other_actor)
    with pytest.raises(RuleViolation):
        service.create(actor, project, "create", CREATE)
