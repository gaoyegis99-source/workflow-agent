import hashlib
from concurrent.futures import ThreadPoolExecutor

import pytest
from platform_contracts.service import ServiceFault
from platform_data.adapters.blobs import Blobs
from platform_data.adapters.repository import Repository
from platform_data.application.data import Data
from platform_data.application.publication import Publication


def candidate():
    contents = {}
    members = []
    files = []
    outputs = {}
    for number, name in enumerate(("complete", "trend"), start=1):
        child_files = []
        for part in ("result", "table", "method"):
            content = f"{name}-{part}".encode()
            digest = "sha256:" + hashlib.sha256(content).hexdigest()
            item = {
                "representation_id": part,
                "format": "json",
                "digest": digest,
                "size": len(content),
            }
            child_files.append(item)
            files.append(
                item
                | {
                    "representation_id": f"node{number}--{part}",
                    "original_representation_id": part,
                    "member_id": f"node{number}",
                    "child_execution_id": f"child{number}",
                    "filename": part + ".json",
                }
            )
            contents[f"node{number}--{part}"] = content
        manifest = {
            "execution_id": f"child{number}",
            "attempt_id": f"attempt{number}",
            "task_ref": {"task_id": "task", "spec_revision": 1},
            "target": {"operator_id": name},
            "input": {},
            "parameters": {},
            "semantics": {},
            "fields": [],
            "row_count": 1,
            "files": child_files,
        }
        members.append(
            {
                "member_id": f"node{number}",
                "child_execution_id": f"child{number}",
                "manifest": manifest,
            }
        )
        outputs[name] = {"member_id": f"node{number}", "representation_id": "result"}
    return {
        "kind": "WORKFLOW_RESULT",
        "project_id": "project",
        "execution_id": "workflow",
        "attempt_id": "attempt",
        "task_ref": {"task_id": "task", "spec_revision": 1},
        "target": {},
        "outputs": outputs,
        "members": members,
        "files": files,
    }, contents


def prepared(service_databases, tmp_path):
    database, _ = service_databases
    service = Publication(Repository(database), Blobs(tmp_path))
    manifest, contents = candidate()
    service.prepare("project", "workflow", 1, manifest)
    for identity, content in contents.items():
        service.file("project", "workflow", 1, identity, content)
    return service, Data(service.repo, service.blobs)


def test_workflow_group_becomes_visible_in_one_commit(service_databases, tmp_path):
    service, data = prepared(service_databases, tmp_path)
    assert data.list("project", "assets") == []
    receipt = service.commit("project", "workflow", 1)
    assert receipt["state"] == "COMMITTED" and len(receipt["assets"]) == 2
    assert set(receipt["outputs"]) == {"complete", "trend"}
    assert len(data.list("project", "assets")) == 2
    assert service.commit("project", "workflow", 2) == receipt
    assert service.abort("project", "workflow", 3) == receipt


@pytest.mark.parametrize("fault", ["missing-file", "wrong-output"])
def test_group_rejects_incomplete_or_unbound_output(service_databases, tmp_path, fault):
    database, _ = service_databases
    service = Publication(Repository(database), Blobs(tmp_path))
    manifest, _ = candidate()
    if fault == "missing-file":
        manifest["files"].pop()
    else:
        manifest["outputs"]["trend"]["representation_id"] = "absent"
    with pytest.raises(ServiceFault) as problem:
        service.prepare("project", "workflow", 1, manifest)
    assert problem.value.code == "INVALID_BINDING"


def test_group_commit_abort_race_has_one_fact(service_databases, tmp_path):
    service, data = prepared(service_databases, tmp_path)

    def commit():
        try:
            return service.commit("project", "workflow", 1)["state"]
        except ServiceFault:
            return "ABORTED"

    with ThreadPoolExecutor(max_workers=2) as pool:
        committed = pool.submit(commit)
        cancelled = pool.submit(service.abort, "project", "workflow", 1)
        assert committed.result() == cancelled.result()["state"]
    count = len(data.list("project", "assets"))
    assert count in {0, 2}
