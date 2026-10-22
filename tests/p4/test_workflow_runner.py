import hashlib
import json

from platform_execution.application.workflow_runner import WorkflowRunner


class Repository:
    def __init__(self, children=None):
        self._children = children or []

    def change(self, row, **changes):
        row.update(changes)
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


def test_stopped_engine_without_original_jobstore_is_not_restarted(tmp_path):
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
        "payload": {"expected_spec_revision": 1, "target": {}},
        "runtime": {"engine_epoch": 1},
    }
    result = runner.collect(row, tmp_path)
    assert len(result["runtime"]["manifest"]["members"]) == 2
    assert [item["member_id"] for item in result["runtime"]["manifest"]["outputs"]["items"]] == [
        "node_1",
        "node_2",
    ]
