from pathlib import Path
from types import SimpleNamespace

from platform_core.application.workflow_compiler import compile_package
from platform_infrastructure.cwl import CwlValidator

from tests.authoring.a2.helpers import profile_source


def test_cwltool_container_has_fixed_resources_and_process_isolation(monkeypatch):
    command = []

    def run(args, **_kwargs):
        command.extend(args)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("platform_infrastructure.cwl.subprocess.run", run)
    CwlValidator("sha256:" + "a" * 64).validate(compile_package(profile_source()))

    assert command[0:2] == ["docker", "run"]
    for option, value in {
        "--network": "none",
        "--cpus": "1",
        "--memory": "512m",
        "--pids-limit": "64",
        "--cap-drop": "ALL",
        "--security-opt": "no-new-privileges",
        "--user": "65532:65532",
    }.items():
        position = command.index(option)
        assert command[position + 1] == value
    assert "--read-only" in command


def test_fixed_cwltool_container_accepts_a2_generated_package():
    image = Path(".p4-runtime/cwl-image.txt").read_text(encoding="utf-8").strip()
    assert image.startswith("sha256:")
    CwlValidator(image, timeout=90).validate(compile_package(profile_source()))
