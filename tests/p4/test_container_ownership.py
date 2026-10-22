from types import SimpleNamespace

from tests.p3 import harness


def test_cleanup_rechecks_test_run_label_before_removal(monkeypatch):
    run_id = "a" * 32
    calls = []

    def execute(command, **_kwargs):
        calls.append(command)
        if command[1:4] == ["ps", "-aq", "--filter"]:
            return SimpleNamespace(returncode=0, stdout=b"owned\nhistorical\n")
        if command[1] == "inspect":
            label = run_id if command[-1] == "owned" else "b" * 32
            return SimpleNamespace(returncode=0, stdout=(label + "\n").encode())
        return SimpleNamespace(returncode=0, stdout=b"")

    monkeypatch.setattr(harness.subprocess, "run", execute)
    harness.cleanup_test_containers(run_id)

    removals = [command for command in calls if command[1:4] == ["rm", "-f", "owned"]]
    assert len(removals) == 1
    assert not any(command[1:4] == ["rm", "-f", "historical"] for command in calls)
    assert not any("v7.phase" in part for command in calls for part in command)


def test_cleanup_rejects_unscoped_or_shell_like_identity():
    for value in ("", "short", "*", "run;docker-rm"):
        try:
            harness.cleanup_test_containers(value)
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe test run identity was accepted")
