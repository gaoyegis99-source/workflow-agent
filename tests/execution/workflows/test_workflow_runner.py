import hashlib
import json
import subprocess

import pytest
from platform_contracts.engine_package import fingerprint
from platform_contracts.engine_runtime import OFF_PROFILE_ID, profile
from platform_execution.application.workflow_runner import WorkflowRunner


class Repository:
    def __init__(self, children=None):
        self._children = children or []

    def change(self, row, **changes):
        row.update(changes)
        return row

    def transition(self, row, *, source_phase, runtime, **changes):
        assert row["runtime"]["phase"] == source_phase
        row.update(changes, runtime=runtime)
        return row

    def children(self, execution):
        return self._children

    def append_trace(self, event):
        return True


class Docker:
    image = "sha256:" + "1" * 64

    def inspect(self, execution, epoch):
        return {"Id": "engine", "State": {"Running": False}}

    def check(self, row, info, epoch):
        return None


class Data:
    def json(self, method, path, json):
        assert path.endswith("/abort")
        return {"state": "ABORTED", "execution_id": "execution"}


class Platform:
    pass


class AllowPlatform:
    @staticmethod
    def json(method, path, json):
        assert method == "POST"
        assert path.endswith("/permit")
        return {"decision": "ALLOW"}


class CreateTimeoutDocker:
    image = "sha256:" + "1" * 64

    def __init__(self):
        self.created = None
        self.create_calls = 0
        self.checked = []

    def inspect(self, execution, epoch):
        assert execution == "execution"
        return self.created if epoch == 2 else {"Id": "engine-1"}

    def create(self, row, root, epoch, config):
        self.create_calls += 1
        assert epoch == 2
        assert config == root / "config-2.json"
        self.created = {"Id": "engine-2"}
        raise subprocess.TimeoutExpired(["docker", "create"], 30)

    def check(self, row, info, epoch):
        self.checked.append((info["Id"], epoch))


def engine_payload():
    compiled = {"manifest_digest": "sha256:" + "a" * 64}
    plan = {
        "plan_digest": "sha256:" + "b" * 64,
        "nodes": [],
        "deferred_templates": [],
    }
    body = {
        "schema_version": 1,
        "strategy": "flat_v1",
        "source_package_digest": compiled["manifest_digest"],
        "plan_digest": plan["plan_digest"],
        "entrypoint": "workflow.cwl",
        "files": {
            "workflow.cwl": {
                "cwlVersion": "v1.2",
                "class": "Workflow",
                "steps": {"v7_gather": {"run": "gather.cwl"}},
            }
        },
        "node_map": {},
    }
    return {
        "compiled": compiled,
        "execution_plan": plan,
        "engine_package": body | {"package_digest": fingerprint(body)},
        "expected_spec_revision": 1,
        "target": {},
    }


def test_stopped_engine_without_original_jobstore_is_not_restarted(tmp_path):
    tmp_path.chmod(0o777)
    runner = WorkflowRunner(Repository(), Docker(), Data(), Platform(), "http://127.0.0.1:8000")
    row = {
        "action_id": "action",
        "execution_id": "execution",
        "project_id": "project",
        "task_id": "task",
        "attempt_id": "attempt",
        "digest": "digest",
        "generation": 1,
        "cancel_requested": False,
        "state": "RUNNING",
        "executed": True,
        "runtime": {"phase": "ENGINE_RUNNING", "engine_epoch": 1},
    }
    runner.step(row, tmp_path)
    assert row["state"] == "FAILED"
    assert row["runtime"]["error"] == "ENGINE_EXIT_UNKNOWN"
    assert row["runtime"]["publication"] == "ABORTED"
    assert tmp_path.stat().st_mode & 0o777 == 0o700


def test_half_initialized_jobstore_is_not_treated_as_recoverable(tmp_path):
    (tmp_path / "jobstore" / "shared").mkdir(parents=True)
    runner = WorkflowRunner(Repository(), Docker(), Data(), Platform(), "http://127.0.0.1:8000")
    row = {
        "action_id": "action",
        "execution_id": "execution",
        "project_id": "project",
        "task_id": "task",
        "attempt_id": "attempt",
        "digest": "digest",
        "generation": 1,
        "cancel_requested": False,
        "state": "RUNNING",
        "executed": True,
        "runtime": {"phase": "ENGINE_RUNNING", "engine_epoch": 1},
    }
    runner.step(row, tmp_path)
    assert row["state"] == "FAILED"
    assert row["runtime"]["error"] == "ENGINE_EXIT_UNKNOWN"


def test_restart_adopts_target_epoch_after_create_timeout(tmp_path):
    (tmp_path / "jobstore" / "shared").mkdir(parents=True)
    (tmp_path / "jobstore" / "shared" / "rootJobStoreID").write_text("root")
    (tmp_path / "bridge.token").write_text("token")
    docker = CreateTimeoutDocker()
    runner = WorkflowRunner(Repository(), docker, Data(), AllowPlatform(), "http://127.0.0.1:8000")
    row = {
        "action_id": "action",
        "execution_id": "execution",
        "project_id": "project",
        "task_id": "task",
        "attempt_id": "attempt",
        "digest": "digest",
        "generation": 1,
        "cancel_requested": False,
        "state": "RUNNING",
        "executed": True,
        "payload": engine_payload(),
        "runtime": {
            "phase": "RESTART_READY",
            "engine_epoch": 1,
            "_engine_runtime_profile": profile(OFF_PROFILE_ID),
        },
    }

    with pytest.raises(subprocess.TimeoutExpired):
        runner.step(row, tmp_path)
    assert row["runtime"]["phase"] == "RESTART_READY"

    runner.step(row, tmp_path)

    assert docker.create_calls == 1
    assert docker.checked == [("engine-2", 2)]
    assert row["runtime"]["phase"] == "ENGINE_CREATED"
    assert row["runtime"]["engine_epoch"] == 2
    assert row["container_id"] == "engine-2"


def test_equal_scatter_outputs_keep_distinct_node_provenance(tmp_path):
    content = b"same result for duplicate input values"
    fingerprint = "sha256:" + hashlib.sha256(content).hexdigest()
    children = []
    results = tmp_path / "results"
    results.mkdir()
    values = []
    for number in (1, 2):
        node = f"node_{number}"
        filename = node + "--result"
        (results / filename).write_bytes(content)
        values.append({"class": "File", "path": "/run/work/results/" + filename})
        children.append(
            {
                "action_id": node,
                "execution_id": f"child_{number}",
                "runtime": {
                    "files": [
                        {
                            "representation_id": "result",
                            "filename": "result.json",
                            "format": "json",
                            "digest": fingerprint,
                            "size": len(content),
                        }
                    ],
                    "manifest": {"node": number},
                },
            }
        )
    (tmp_path / "result-1.json").write_text(json.dumps({"items": values}))
    repository = Repository(children)
    runner = WorkflowRunner(repository, Docker(), Data(), Platform(), "http://127.0.0.1:8000")
    row = {
        "execution_id": "root",
        "attempt_id": "attempt",
        "project_id": "project",
        "task_id": "task",
        "payload": engine_payload(),
        "runtime": {"phase": "ENGINE_RUNNING", "engine_epoch": 1},
    }
    result = runner.output_collector.run(row, tmp_path)
    assert len(result["runtime"]["manifest"]["members"]) == 2
    assert [item["member_id"] for item in result["runtime"]["manifest"]["outputs"]["items"]] == [
        "node_1",
        "node_2",
    ]
