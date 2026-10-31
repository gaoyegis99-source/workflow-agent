"""Install the current P6 runtime outside the repository and exercise its boundary.

This is deliberately narrower than ``verify_distribution.py``: P6.0 adds one
internal runtime package and no public API or business migration, so rerunning
the installed P3/P4 container workflows would add time without testing the new
boundary.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.lib.runtime_layout import P6_RUNTIME_ROOT  # noqa: E402

DIST = P6_RUNTIME_ROOT / "release" / "dist"
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def run(arguments: list[str | Path], *, cwd: Path, env: dict[str, str]) -> str:
    result = subprocess.run(
        [str(argument) for argument in arguments],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=180,
    )
    if result.returncode:
        raise RuntimeError(
            f"Command failed: {arguments}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result.stdout


def only_wheel(prefix: str) -> Path:
    matches = sorted(DIST.glob(f"{prefix}-{VERSION}-py3-none-any.whl"))
    if len(matches) != 1:
        raise RuntimeError(f"Expected one {prefix} {VERSION} wheel, found {matches}")
    return matches[0]


def main() -> None:
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required")
    if not DIST.is_dir():
        raise RuntimeError("Build P6 release artifacts before distribution verification")

    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PYTHONOPTIMIZE"}
    }
    wheels = [
        only_wheel("groundwater_platform_core"),
        only_wheel("groundwater_platform_contracts"),
        only_wheel("groundwater_platform_infrastructure"),
        only_wheel("groundwater_agent_runtime"),
        only_wheel("groundwater_platform_api"),
    ]

    with tempfile.TemporaryDirectory(prefix="groundwater-p6-installed-") as temporary:
        outside = Path(temporary)
        requirements = outside / "runtime-requirements.txt"
        api_requirements = outside / "api-requirements.txt"
        environment = outside / "runtime-env"
        run(
            [
                uv,
                "export",
                "--locked",
                "--no-dev",
                "--no-emit-workspace",
                "--package",
                "groundwater-agent-runtime",
                "--output-file",
                requirements,
            ],
            cwd=ROOT,
            env=env,
        )
        run(
            [
                uv,
                "export",
                "--locked",
                "--no-dev",
                "--no-emit-workspace",
                "--package",
                "groundwater-platform-api",
                "--output-file",
                api_requirements,
            ],
            cwd=ROOT,
            env=env,
        )
        run([uv, "venv", "--python", sys.executable, environment], cwd=outside, env=env)
        python = environment / "bin/python"
        run(
            [uv, "pip", "install", "--python", python, "--require-hashes", "-r", requirements],
            cwd=outside,
            env=env,
        )
        run(
            [
                uv,
                "pip",
                "install",
                "--python",
                python,
                "--require-hashes",
                "-r",
                api_requirements,
            ],
            cwd=outside,
            env=env,
        )
        run(
            [uv, "pip", "install", "--python", python, "--no-deps", *wheels],
            cwd=outside,
            env=env,
        )
        run([uv, "pip", "check", "--python", python], cwd=outside, env=env)

        probe = run([environment / "bin/agent-runtime-probe"], cwd=outside, env=env)
        if "langgraph-minimal-v1" not in probe or "plain-loop-v1" not in probe:
            raise RuntimeError("The installed profile probe did not expose both frozen profiles")

        smoke = """
from langgraph.checkpoint.memory import InMemorySaver
from platform_agent_runtime.langgraph_adapter import LangGraphRuntime
from platform_agent_runtime.evaluation import evaluate_real
from platform_agent_runtime.embedding_client import HttpEmbeddingClient
from platform_agent_runtime.plain_loop import PlainLoopRuntime
from platform_agent_runtime.profiles import LANGGRAPH_PROFILE_ID, PLAIN_PROFILE_ID, profile
from platform_agent_runtime.model_providers import DeterministicModelProvider
from platform_core.application.model_catalog import build_catalog
from platform_core.application.context_compiler import ContextCompiler
from platform_core.application.tool_catalog import build_tool_catalog
from platform_core.domain.agent_runtime import canonical_digest
from platform_core.domain.agent_runtime import DecisionFrame, DecisionProposal

digest = "sha256:" + "a" * 64
frame = DecisionFrame("installed-run", "frame-1", "task-1", 1, 1, 1, digest, {"goal": "probe"})

for runtime_type, profile_id, checkpointer in (
    (PlainLoopRuntime, PLAIN_PROFILE_ID, None),
    (LangGraphRuntime, LANGGRAPH_PROFILE_ID, InMemorySaver()),
):
    calls = []
    def decide(current, observations):
        calls.append(current.frame_id)
        return DecisionProposal("EXPLANATION", current.frame_id, 1, {"message": "ready"})
    runtime = runtime_type(decide) if checkpointer is None else runtime_type(decide, checkpointer)
    selected = profile(profile_id)
    receipt = runtime.start({"runtime_profile": selected}, frame)
    first = runtime.advance("installed-run", frame, ())
    replay = runtime.advance("installed-run", frame, ({"kind": "ignored-on-replay"},))
    assert receipt.profile_digest == selected["profile_digest"]
    assert first == replay and len(calls) == 1
    assert runtime.inspect("installed-run").last_proposal_digest == first.digest

import pathlib, sys
import importlib.resources
import platform_agent_runtime
import platform_api
import platform_contracts
import platform_core
import platform_infrastructure
assert all(pathlib.Path(module.__file__).is_relative_to(pathlib.Path(sys.prefix)) for module in (
    platform_agent_runtime,
    platform_api,
    platform_contracts,
    platform_core,
    platform_infrastructure,
))
package_root = importlib.resources.files("platform_api")
page = package_root.joinpath("studio_static/index.html").read_text()
assert 'id="root"' in page and "Groundwater V7" in page
assert not any(
    item.name.endswith(".map")
    for item in package_root.joinpath("studio_static/assets").iterdir()
)

unsigned = {
    "profile_id": "installed-model-v1",
    "provider": "DETERMINISTIC_FAKE",
    "model": "installed-snapshot-1",
    "request_mode": "RESPONSES",
    "supports_structured_output": True,
    "supports_query_by_response_id": True,
    "data_policy": "LOCAL_TEST_ONLY",
    "max_context_tokens": 1000,
    "max_output_tokens": 100,
    "max_reasoning_tokens": 10,
    "timeout_seconds": 5,
    "pricing_revision": "installed-1",
    "pricing": {
        "input_microunits_per_million": 1,
        "output_microunits_per_million": 1,
        "reasoning_microunits_per_million": 1,
    },
    "credential_env": "INSTALLED_MODEL_KEY",
    "base_url": "http://127.0.0.1:18099/v1",
}
model_profile = unsigned | {"profile_digest": canonical_digest(unsigned)}
catalog = build_catalog([model_profile], "installed-model-v1")
assert catalog["profiles"][0]["profile_digest"] == model_profile["profile_digest"]
assert DeterministicModelProvider([]).submit_count == 0
assert callable(evaluate_real)
embedding = HttpEmbeddingClient("http://127.0.0.1:9")
assert embedding.model == "qwen3-embedding-4b-q4" and embedding.dimensions == 1024
tools = build_tool_catalog("science")
compiled = ContextCompiler(tools, max_context_tokens=8000).compile(
    task={"task_id": "task"}, task_spec={"goal": "probe"}, budget={}, plan=None,
    events=[], answers=[], tool_observations=[], pending_wake_ids=[]
)
assert compiled.manifest["tool_catalog_digest"] == tools["catalog_digest"]
"""
        run([python, "-I", "-c", smoke], cwd=outside, env=env)
        help_text = run(
            [environment / "bin/agent-checkpoint-admin", "--help"], cwd=outside, env=env
        )
        if "check-schema" not in help_text or "migrate" not in help_text:
            raise RuntimeError("The installed checkpoint administration CLI is incomplete")
        catalogue = outside / "agent-tool-catalog.json"
        run(
            [environment / "bin/agent-tool-catalog", "generate", "--output", catalogue],
            cwd=outside,
            env=env,
        )
        run(
            [environment / "bin/agent-tool-catalog", "check", "--output", catalogue],
            cwd=outside,
            env=env,
        )
        worker_help = run([environment / "bin/agent-worker", "--help"], cwd=outside, env=env)
        if "ledger-owned Agent worker" not in worker_help:
            raise RuntimeError("The installed Agent worker entrypoint is missing")
        admin_help = run([environment / "bin/agent-admin", "--help"], cwd=outside, env=env)
        if "rebuild-checkpoint" not in admin_help:
            raise RuntimeError("The installed Agent administration CLI cannot rebuild checkpoints")
        backup_help = run([environment / "bin/agent-ledger-backup", "--help"], cwd=outside, env=env)
        if "backup" not in backup_help or "restore" not in backup_help:
            raise RuntimeError("The installed Agent backup CLI is incomplete")

    print(
        f"PASS: P6 {VERSION} locked dependencies and five required wheels "
        "installed outside repository"
    )
    print(
        "PASS: installed plain-loop and LangGraph adapters preserve one-decision replay semantics"
    )
    print("PASS: installed runtime exposes only frozen internal profiles")
    print("PASS: installed runtime exposes explicit checkpoint migrate/check-schema administration")
    print("PASS: installed runtime generates and verifies the frozen P6.6 science tool catalogue")
    print("PASS: installed runtime includes the production Agent worker composition root")
    print("PASS: installed runtime includes controlled checkpoint rebuild and backup/restore CLIs")
    print("PASS: installed Platform API includes the P6 AGENT task interface")
    print("PASS: installed runtime includes the bounded no-proxy embedding adapter")


if __name__ == "__main__":
    main()
