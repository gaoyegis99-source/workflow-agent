import importlib.util
import json
from pathlib import Path

BRIDGE_PATH = Path(__file__).resolve().parents[2] / "runtimes/cwl/bridge.py"
SPEC = importlib.util.spec_from_file_location("v7_cwl_bridge", BRIDGE_PATH)
bridge = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(bridge)


def process(root, pid, *, args=None, parent=1):
    directory = root / str(pid)
    directory.mkdir()
    if args is not None:
        (directory / "cmdline").write_bytes(b"\0".join(item.encode() for item in args) + b"\0")
    # The bridge parser reads the second field after the final ')'.
    (directory / "stat").write_text(f"{pid} (worker name) S {parent} 0 0 0\n")


def test_toil_job_id_is_best_effort(tmp_path):
    process(tmp_path, 30, args=["python", "bridge.py"], parent=20)
    process(
        tmp_path,
        20,
        args=["python", "_toil_worker", "ignored", "file:/run/work/jobstore", "job-A"],
    )
    assert bridge.engine_job_id(tmp_path, 30) == "job-A"


def test_missing_or_unexpected_proc_context_returns_none(tmp_path):
    assert bridge.engine_job_id(tmp_path, 999) is None

    process(tmp_path, 30, args=["python", "bridge.py"], parent=20)
    process(tmp_path, 20, args=["_toil_worker", "ignored"], parent=1)
    assert bridge.engine_job_id(tmp_path, 30) is None

    (tmp_path / "20" / "cmdline").write_bytes(
        b"python\0_toil_worker\0ignored\0file:/other/jobstore\0job-B\0"
    )
    assert bridge.engine_job_id(tmp_path, 30) is None


def test_engine_binding_never_changes_stable_node_identity():
    execution = "execution_1"
    path = ["outer", "leaf"]
    indices = [2, 3]
    expected = bridge.node_identity(execution, path, indices)
    assert expected == bridge.node_identity(execution, path, indices)
    assert expected != bridge.node_identity(execution, path, [2, 4])
    assert expected != bridge.node_identity("execution_2", path, indices)
    # Engine bindings are deliberately absent from the identity input.
    assert json.loads(json.dumps({"node": expected, "engine_job_id": None}))["node"] == expected
