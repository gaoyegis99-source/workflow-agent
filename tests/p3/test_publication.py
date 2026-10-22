import hashlib
from concurrent.futures import ThreadPoolExecutor

import pytest
from platform_contracts.service import ServiceFault
from platform_data.adapters.blobs import Blobs
from platform_data.adapters.repository import Repository
from platform_data.application.data import Data
from platform_data.application.publication import Publication


def candidate():
    content = b"fixed-output"
    return {
        "project_id": "project",
        "execution_id": "exec",
        "attempt_id": "attempt",
        "task_ref": {"task_id": "task", "spec_revision": 1},
        "target": {},
        "input": {},
        "parameters": {},
        "semantics": {},
        "fields": [],
        "row_count": 1,
        "files": [
            {
                "representation_id": name,
                "format": "json",
                "size": len(content),
                "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
            }
            for name in ["result", "table", "method"]
        ],
    }, content


def prepared(service_databases, tmp_path):
    db, _ = service_databases
    repo, blobs = Repository(db), Blobs(tmp_path)
    service = Publication(repo, blobs)
    manifest, content = candidate()
    service.prepare("project", "exec", 1, manifest)
    for name in ["result", "table", "method"]:
        service.file("project", "exec", 1, name, content)
    return service, Data(repo, blobs)


def test_atomic_commit_replay_and_abort_after_commit(service_databases, tmp_path):
    service, data = prepared(service_databases, tmp_path)
    assert data.list("project", "assets") == []
    receipt = service.commit("project", "exec", 1)
    assert service.commit("project", "exec", 2) == receipt
    assert service.abort("project", "exec", 3) == receipt
    assert len(data.list("project", "assets")) == 1


def test_abort_tombstone_and_old_generation(service_databases, tmp_path):
    service, data = prepared(service_databases, tmp_path)
    service.abort("project", "exec", 2)
    with pytest.raises(ServiceFault):
        service.commit("project", "exec", 1)
    with pytest.raises(ServiceFault):
        service.commit("project", "exec", 3)
    manifest, _ = candidate()
    service.abort("project", "early", 1)
    with pytest.raises(ServiceFault):
        service.prepare("project", "early", 2, manifest | {"execution_id": "early"})
    assert data.list("project", "assets") == []


def test_commit_abort_race_is_one_atomic_fact(service_databases, tmp_path):
    service, data = prepared(service_databases, tmp_path)

    def commit():
        try:
            return service.commit("project", "exec", 1)["state"]
        except ServiceFault:
            return "ABORTED"

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(commit)
        b = pool.submit(service.abort, "project", "exec", 1)
        committed, cancelled = a.result(), b.result()["state"]
    assert committed == cancelled
    assert len(data.list("project", "assets")) == (1 if committed == "COMMITTED" else 0)
