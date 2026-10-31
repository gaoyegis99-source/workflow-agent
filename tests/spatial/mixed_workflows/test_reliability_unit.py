import hashlib
from pathlib import Path

import pytest
import sqlalchemy as sa
from platform_contracts.service import ServiceFault
from platform_execution.adapters.repository import Repository as ExecutionRepository
from platform_execution.adapters.tables import executions, remote_calls
from platform_execution.application.components import RemoteExecutionDriver
from platform_execution.application.workflow_runner import WorkflowRunner


def sha256(content):
    return "sha256:" + hashlib.sha256(content).hexdigest()


class RemoteRepository:
    def change_remote(self, row, **changes):
        row.setdefault("remote_call", {}).update(changes)
        return True

    def change(self, row, **changes):
        row.update(changes)
        return True

    def transition(self, row, *, source_phase, runtime, **changes):
        assert row["runtime"]["phase"] == source_phase
        row.update(changes, runtime=runtime)
        return True


class OutputClient:
    def __init__(self, contents, missing=()):
        self.contents = contents
        self.missing = set(missing)

    def output(self, action, path, target, maximum, expected_digest, expected_size):
        name = Path(path).name
        if name in self.missing:
            return None
        content = self.contents[name]
        target = Path(target)
        if not target.exists():
            target.write_bytes(content)
        assert len(content) == expected_size <= maximum
        assert sha256(content) == expected_digest
        return "application/json"


def remote_case(tmp_path):
    contents = {"summary": b'{"summary":true}', "method": b'{"method":true}'}
    outputs = {
        name: {
            "path": f"/v1/actions/action/outputs/{name}",
            "digest": sha256(content),
            "size": len(content),
            "media_type": "application/json",
        }
        for name, content in contents.items()
    }
    row = {
        "action_id": "action",
        "runtime": {
            "phase": "REMOTE_TRACKING",
            "remote": {"remote_execution_id": "remote"},
            "implementation_profile": {
                "outputs": {"summary": "summary.json", "method": "method.json"}
            },
        },
    }
    call = {
        "remote_revision": 4,
        "binding": {"limits": {"output_bytes": 1024}},
        "observation": {"outputs": outputs},
    }
    root = tmp_path / "execution"
    (root / "output").mkdir(parents=True)
    driver = object.__new__(RemoteExecutionDriver)
    driver.repo = RemoteRepository()

    class Runtime:
        @staticmethod
        def change(received, **changes):
            received["runtime"] = received["runtime"] | changes
            return received

    driver.execution = Runtime()
    driver.output_collector = type(
        "Collector",
        (),
        {"run": staticmethod(lambda received, received_root: (received, received_root))},
    )()
    return driver, row, call, root, contents


def test_remote_files_are_not_promoted_until_the_whole_set_is_verified(tmp_path):
    driver, row, call, root, contents = remote_case(tmp_path)
    with pytest.raises(OSError):
        driver.collect_remote_outputs(row, root, call, OutputClient(contents, missing={"method"}))
    assert list((root / "output").iterdir()) == []
    assert (root / "remote-incoming/4/summary.json").is_file()
    assert row["runtime"]["remote"]["failure_code"] == "REMOTE_OUTPUT_NOT_VISIBLE"

    driver.collect_remote_outputs(row, root, call, OutputClient(contents))
    assert {path.name for path in (root / "output").iterdir()} == {
        "summary.json",
        "method.json",
    }
    assert not (root / "remote-incoming").exists()
    assert row["remote_call"]["phase"] == "OUTPUT_VERIFIED"


def test_remote_output_manifest_must_have_exact_ports_and_digests(tmp_path):
    driver, row, call, root, contents = remote_case(tmp_path)
    call["observation"]["outputs"].pop("method")
    with pytest.raises(ServiceFault) as missing:
        driver.collect_remote_outputs(row, root, call, OutputClient(contents))
    assert missing.value.code == "INVALID_BINDING"
    assert list((root / "output").iterdir()) == []

    driver, row, call, root, contents = remote_case(tmp_path / "digest")
    call["observation"]["outputs"]["summary"]["digest"] = "sha256:not-a-digest"
    with pytest.raises(ServiceFault) as digest:
        driver.collect_remote_outputs(row, root, call, OutputClient(contents))
    assert digest.value.code == "INVALID_BINDING"
    assert list((root / "output").iterdir()) == []


class PublicationRepository:
    def __init__(self, children, *, cancelled=False):
        self._children = children
        self.cancelled = cancelled

    def children(self, execution):
        return self._children

    def get(self, **identity):
        return {"cancel_requested": self.cancelled}

    def change(self, row, **changes):
        row.update(changes)
        return True

    def transition(self, row, *, source_phase, runtime, **changes):
        assert row["runtime"]["phase"] == source_phase
        row.update(changes, runtime=runtime)
        return True

    def append_trace(self, event):
        return True


class PublicationData:
    def __init__(self, abort_state="ABORTED"):
        self.abort_state = abort_state
        self.calls = []

    def json(self, method, path, json):
        self.calls.append((method, path))
        if path.endswith("/prepare"):
            return {"state": "PREPARED"}
        if path.endswith("/commit"):
            return {"state": "COMMITTED", "assets": ["asset"]}
        assert path.endswith("/abort")
        return {"state": self.abort_state, "assets": ["asset"]}

    def request(self, method, path, **kwargs):
        self.calls.append((method, path))
        return object()


def publication_case(tmp_path, child_state="SUCCEEDED"):
    content = b"trusted-private-output"
    child = {
        "action_id": "node",
        "execution_id": "child",
        "state": child_state,
        "runtime": {
            "publication": "PRIVATE",
            "manifest": {"kind": "SPATIAL_SUMMARY"},
            "files": [
                {
                    "representation_id": "summary",
                    "filename": "summary.json",
                    "format": "json",
                    "digest": sha256(content),
                    "size": len(content),
                }
            ],
        },
    }
    root = tmp_path / "root"
    root.mkdir()
    output = tmp_path / "child/output"
    output.mkdir(parents=True)
    (output / "summary.json").write_bytes(content)
    manifest = {
        "members": [
            {
                "member_id": "node",
                "child_execution_id": "child",
                "manifest": child["runtime"]["manifest"],
            }
        ],
        "files": [
            child["runtime"]["files"][0]
            | {
                "member_id": "node",
                "child_execution_id": "child",
                "original_representation_id": "summary",
            }
        ],
    }
    row = {
        "action_id": "parent",
        "execution_id": "root",
        "project_id": "project",
        "generation": 1,
        "runtime": {"phase": "PUBLISHING", "manifest": manifest},
    }
    return row, child, root


def test_parent_revalidates_private_children_before_publication(tmp_path):
    row, child, root = publication_case(tmp_path, child_state="FAILED")
    data = PublicationData()
    runner = WorkflowRunner(
        PublicationRepository([child]), object(), data, object(), "http://execution"
    )
    with pytest.raises(ServiceFault) as problem:
        runner.artifact_publisher.run(row, root)
    assert problem.value.code == "INVALID_BINDING"
    assert data.calls == []


def test_parent_cancellation_is_rechecked_before_publication_prepare(tmp_path):
    row, child, root = publication_case(tmp_path)
    row.update(state="RUNNING")
    data = PublicationData()
    runner = WorkflowRunner(
        PublicationRepository([child], cancelled=True), object(), data, object(), "http://execution"
    )
    runner.artifact_publisher.run(row, root)
    assert row["state"] == "CANCELLED"
    assert row["runtime"]["cancellation"] == {"state": "ABORTED_BEFORE_COMMIT"}
    assert data.calls == [("POST", "/internal/projects/project/publications/root/abort")]


@pytest.mark.parametrize(
    ("data_state", "expected_state", "cancellation"),
    [
        ("ABORTED", "CANCELLED", "ABORTED_BEFORE_COMMIT"),
        ("COMMITTED", "SUCCEEDED", "TOO_LATE_COMMITTED"),
    ],
)
def test_publication_cancel_race_preserves_the_winning_fact(
    tmp_path, data_state, expected_state, cancellation
):
    row, child, _ = publication_case(tmp_path)
    row.update(state="RUNNING")
    data = PublicationData(abort_state=data_state)
    runner = WorkflowRunner(
        PublicationRepository([child]), object(), data, object(), "http://execution"
    )
    runner.lifecycle.abort(row, "CANCELLED", "CANCELLED")
    assert row["state"] == expected_state
    assert row["runtime"]["publication"] == data_state
    assert row["runtime"]["cancellation"] == {"state": cancellation}


def test_remote_execution_and_ledger_rollback_as_one_transaction(service_databases):
    database = service_databases[1]
    repository = ExecutionRepository(database)
    action = "p432-ledger-rollback"
    payload = {"project_id": "project", "task_id": "task", "kind": "OPERATOR"}
    function = "p432_reject_remote_call"
    trigger = "p432_reject_remote_call_trigger"
    with database.engine.begin() as connection:
        connection.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger} ON remote_calls"))
        connection.execute(sa.text(f"DROP FUNCTION IF EXISTS {function}()"))
        connection.execute(
            sa.text(
                f"CREATE FUNCTION {function}() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN IF NEW.action_id = 'p432-ledger-rollback' THEN "
                "RAISE EXCEPTION 'injected remote ledger failure'; END IF; RETURN NEW; END $$"
            )
        )
        connection.execute(
            sa.text(
                f"CREATE TRIGGER {trigger} BEFORE INSERT ON remote_calls "
                f"FOR EACH ROW EXECUTE FUNCTION {function}()"
            )
        )
    try:
        with pytest.raises(sa.exc.DBAPIError):
            repository.create(
                action,
                payload,
                invocation_key=action,
                remote_binding={"service_binding_id": "peer"},
                remote_request_digest="sha256:" + "1" * 64,
            )
    finally:
        with database.engine.begin() as connection:
            connection.execute(sa.text(f"DROP TRIGGER IF EXISTS {trigger} ON remote_calls"))
            connection.execute(sa.text(f"DROP FUNCTION IF EXISTS {function}()"))
    with database.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(sa.func.count())
                .select_from(executions)
                .where(executions.c.action_id == action)
            )
            == 0
        )
        assert (
            connection.scalar(
                sa.select(sa.func.count())
                .select_from(remote_calls)
                .where(remote_calls.c.action_id == action)
            )
            == 0
        )


def test_existing_remote_execution_without_ledger_is_not_repaired(service_databases):
    database = service_databases[1]
    repository = ExecutionRepository(database)
    action = "p432-missing-ledger"
    payload = {"project_id": "project", "task_id": "task", "kind": "OPERATOR"}
    repository.create(action, payload, invocation_key=action)
    with pytest.raises(ServiceFault) as problem:
        repository.create(
            action,
            payload,
            invocation_key=action,
            remote_binding={"service_binding_id": "peer"},
            remote_request_digest="sha256:" + "1" * 64,
        )
    assert problem.value.code == "INVALID_BINDING"
    with database.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.select(sa.func.count())
                .select_from(remote_calls)
                .where(remote_calls.c.action_id == action)
            )
            == 0
        )
