import json

import pytest
from fastapi.testclient import TestClient
from platform_api.adapters.http.authoring_import import parse_source
from platform_api.app import create_app
from platform_core.application.control import ControlService
from platform_core.domain.authoring_graph import validate_source_envelope
from platform_core.domain.control import RuleViolation

from tests.authoring.a5.helpers import advanced_source


def test_json_and_yaml_import_have_the_same_canonical_source():
    source = advanced_source()
    parsed_json = parse_source(json.dumps(source), "JSON")
    parsed_yaml = parse_source(json.dumps(source), "YAML")
    assert parsed_json == parsed_yaml == source
    validate_source_envelope(parsed_json)


@pytest.mark.parametrize(
    ("content", "source_format"),
    [
        ('{"profile":"a","profile":"b"}', "JSON"),
        ("profile: a\nprofile: b\n", "YAML"),
        ("value: &anchor [1, 2]\ncopy: *anchor\n", "YAML"),
        ("value: !python/object:os.system echo\n", "YAML"),
        ("value: 2026-10-16\n", "YAML"),
        ("value: .nan\n", "YAML"),
        ('{"value":9007199254740993}', "JSON"),
    ],
)
def test_import_rejects_duplicate_keys_aliases_anchors_and_custom_tags(content, source_format):
    with pytest.raises(RuleViolation) as raised:
        parse_source(content, source_format)
    assert raised.value.code == "MALFORMED_REQUEST"


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("documents", "main", "$import"), "https://example.invalid/workflow.cwl"),
        (("documents", "main", "steps", "each", "baseCommand"), "sh"),
        (("documents", "main", "steps", "each", "expression"), "$(1 + 1)"),
        (("documents", "main", "steps", "each", "path"), "/etc/passwd"),
        (("documents", "main", "steps", "each", "hints"), {"DockerRequirement": {}}),
    ],
)
def test_imported_envelope_rejects_executable_or_external_features(path, value):
    source = advanced_source()
    current = source
    for component in path[:-1]:
        current = current[component]
    current[path[-1]] = value
    with pytest.raises(RuleViolation) as raised:
        validate_source_envelope(source)
    assert raised.value.code == "UNSUPPORTED_TARGET"


def test_import_http_is_authorized_and_does_not_persist_a_revision(database, owner):
    actor, token = owner
    project = ControlService(database).create_project(actor, "a5-import", "A5 import")
    prefix = f"/api/v1/projects/{project['project_id']}"
    body = {"format": "JSON", "content": json.dumps(advanced_source())}
    with TestClient(create_app(database)) as client:
        response = client.post(
            prefix + "/authoring/imports",
            headers={"Authorization": "Bearer " + token},
            json=body,
        )
        assert response.status_code == 200, response.text
        imported = response.json()
        assert imported["source"] == advanced_source()
        assert imported["source_digest"].startswith("sha256:")
        assert imported["editable"] is True
        assert client.post(prefix + "/authoring/imports", json=body).status_code == 401
        unresolved = advanced_source()
        unresolved["documents"]["main"]["steps"]["each"]["run"] = "missing"
        rejected = client.post(
            prefix + "/authoring/imports",
            headers={"Authorization": "Bearer " + token},
            json={"format": "JSON", "content": json.dumps(unresolved)},
        )
        assert rejected.status_code == 422
        assert rejected.json()["code"] == "INVALID_BINDING"
        assert (
            client.get(
                prefix + "/workflow-definitions", headers={"Authorization": "Bearer " + token}
            ).json()["items"]
            == []
        )
