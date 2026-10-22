# ruff: noqa: E501
"""Validate the completed P6.0 gates and write durable release evidence."""

from __future__ import annotations

import hashlib
import json
import platform
import tomllib
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.8.0"
REQUIRED_CASES = {
    "test_adapter_yields_business_action_without_executing_it",
    "test_closed_checkpoint_store_reports_unavailable_without_running_decision",
    "test_interrupt_is_idempotent_and_stops_future_decisions[0]",
    "test_interrupt_is_idempotent_and_stops_future_decisions[1]",
    "test_plain_and_langgraph_have_the_same_yield_and_replay_semantics",
    "test_postgres_checkpoint_survives_adapter_reconstruction",
    "test_profiles_are_exact_and_langgraph_is_selected",
    "test_same_frame_identity_with_other_content_is_a_conflict[0]",
    "test_same_frame_identity_with_other_content_is_a_conflict[1]",
}


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def suite(path: Path) -> tuple[dict[str, int | float], list[dict[str, str]]]:
    root = ET.parse(path).getroot()
    cases = [case.attrib for case in root.findall(".//testcase")]
    return (
        {
            "tests": len(cases),
            "failures": len(root.findall(".//failure")),
            "errors": len(root.findall(".//error")),
            "skipped": len(root.findall(".//skipped")),
            "seconds": round(sum(float(case.get("time", 0)) for case in cases), 3),
        },
        cases,
    )


def require_clean(result: dict[str, int | float], minimum: int, label: str) -> None:
    bad = any(result[key] for key in ("failures", "errors", "skipped"))
    if result["tests"] < minimum or bad:
        raise RuntimeError(f"{label} gate is incomplete: {result}")


def main() -> None:
    verification = ROOT / ".p6-runtime" / "verification"
    release = ROOT / ".p6-runtime" / "release"
    focused, focused_cases = suite(verification / "p60-tests.xml")
    full, _ = suite(verification / "all-tests.xml")
    require_clean(focused, 168, "P6.0 focused")
    require_clean(full, 680, "full regression")
    p60_cases = [
        case for case in focused_cases if case.get("classname", "").startswith("tests.p60")
    ]
    names = {case["name"] for case in p60_cases}
    if len(p60_cases) < 27 or not REQUIRED_CASES <= names:
        raise RuntimeError(
            "Required runtime identity, replay, isolation or checkpoint cases are missing"
        )

    distribution = (verification / "distribution.log").read_text()
    for expected in (
        "locked dependencies and three required wheels installed outside repository",
        "one-decision replay semantics",
        "only frozen internal profiles",
    ):
        if expected not in distribution:
            raise RuntimeError(f"Repository-external distribution gate is missing: {expected}")

    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    packages = {
        item["name"]: item["version"]
        for item in lock["package"]
        if item["name"].startswith("groundwater-") and "editable" in item["source"]
    }
    if len(packages) != 10 or set(packages.values()) != {VERSION}:
        raise RuntimeError("All ten workspace packages must be frozen at 0.8.0")
    dependencies = {
        item["name"]: item["version"]
        for item in lock["package"]
        if item["name"] in {"langgraph", "langgraph-checkpoint-postgres"}
    }
    expected_dependencies = {
        "langgraph": "1.2.15",
        "langgraph-checkpoint-postgres": "3.2.0",
    }
    if dependencies != expected_dependencies:
        raise RuntimeError(f"Unexpected runtime dependencies: {dependencies}")

    artifacts = sorted((release / "dist").glob("*.whl")) + sorted(
        (release / "dist").glob("*.tar.gz")
    )
    if len(artifacts) != 20 or any(VERSION not in artifact.name for artifact in artifacts):
        raise RuntimeError("The ten wheel and ten source distributions are incomplete")
    profiles = json.loads((verification / "runtime-profiles.json").read_text())
    selected = next(value for value in profiles if value["profile_id"] == "langgraph-minimal-v1")
    if selected["checkpoint_kind"] != "POSTGRES" or selected["framework_version"] != "1.2.15":
        raise RuntimeError("The selected runtime profile is not the tested PostgreSQL profile")

    evidence = {
        "phase": "P6.0-runtime-selection",
        "version": VERSION,
        "status": "PASS",
        "finished_at": datetime.now(UTC).isoformat(),
        "commands": [
            "bash scripts/check_p6.sh --phase runtime",
            "uv run --locked pytest -q --tb=short --junitxml=.p6-runtime/verification/all-tests.xml",
            "uv run --locked python scripts/verify_p6_runtime_distribution.py",
        ],
        "p60_tests": {"tests": len(p60_cases), "failures": 0, "errors": 0, "skipped": 0},
        "focused_regression": focused,
        "full_regression": full,
        "packages": packages,
        "runtime_dependencies": dependencies,
        "runtime_profiles": profiles,
        "selected_profile": selected["profile_id"],
        "release_artifacts": {
            artifact.name: {"bytes": artifact.stat().st_size, "sha256": digest(artifact)}
            for artifact in artifacts
        },
        "lock_digest": digest(ROOT / "uv.lock"),
        "schemas": {
            "platform": "0010_checker_runs",
            "data": "0004_document_assets",
            "execution": "0006_remote_reliability",
            "knowledge": "0002_lifecycle_outbox",
            "agent_checkpoint": "LangGraph-managed; business schema deferred to P6.1",
        },
        "python": platform.python_version(),
        "required_cases": sorted(REQUIRED_CASES),
        "limits": [
            "No public AGENT task or agent HTTP route is enabled in P6.0.",
            "No real model provider, business tool execution or model budget is enabled.",
            "The plain-loop adapter is a semantic reference; LangGraph with PostgreSQL checkpointing is selected.",
            "P4 long performance experiments were not repeated because execution scheduling did not change.",
        ],
    }
    (ROOT / "docs/verification/p60-runtime-selection-evidence.json").write_text(
        json.dumps(evidence, indent=2, ensure_ascii=False) + "\n"
    )
    report = f"""# P6.0 / 0.8.0 Agent 运行时选型报告

状态：**PASS**  
完成时间：{evidence["finished_at"]}

## 完成内容

- 新增只依赖标准库的 `DecisionFrame`、`DecisionProposal`、RuntimePort、ModelPort 和 ToolTransport 边界；运行时只能返回提议，不能直接执行业务工具。
- 新增 `plain-loop-v1` 语义基线和 `langgraph-minimal-v1` 最小适配器；两者保持相同的单次决策、重放、唤醒、中断和可见轨迹语义。
- 选定 LangGraph `1.2.15` 与 PostgreSQL checkpointer `3.2.0`。真实 PostgreSQL 验证了适配器对象重建后复用原 checkpoint 和原 proposal。
- profile、框架版本、checkpoint 类型与控制模式均使用规范摘要冻结；客户端不能选择浮动工具版本或令运行时自动调用工具。
- 新增第十个发行包 `groundwater-agent-runtime`，十个包统一为 `0.8.0`，并在仓库外以锁定依赖完成安装和运行探针。

## 测试结果

- P6.0 专项：{len(p60_cases)} passed，0 failed，0 skipped。
- 契约、架构与 P6.0 聚焦回归：{focused["tests"]} passed，0 failed，0 skipped，用时 {focused["seconds"]} 秒。
- 完整回归：{full["tests"]} passed，0 failed，0 skipped，用时 {full["seconds"]} 秒。
- 十个 wheel、十个 sdist 构建成功；仓库外安装、依赖检查、profile probe 和两个适配器重放语义通过。

## 问题处理

- LangGraph 1.2 会把非空 `checkpoint_ns` 解释为子图路径。顶层运行时改用固定前缀、profile 摘要和 AgentRun ID 组成 `thread_id`，避免恢复时出现 `Subgraph ... not found`。
- `ToolRequest` 增加独立版本校验，拒绝 `latest`，避免绕过已有版本化契约的固定版本要求。

## 范围与结论

P6.0 只冻结运行时边界和适配器选择，不开放公开 AGENT Task，不调用真实模型，不执行 Data、Knowledge 或 Execution 工具，也不新增 Platform 业务迁移。P4 调度代码未变化，因此未重复长性能矩阵。

P6.0 通过并结束。本阶段形成的 RuntimePort 是 P6.1 确定性 Agent ledger 的基础；P6.1 尚未实施。
"""
    (ROOT / "docs/verification/p60-runtime-selection-report.md").write_text(report)
    print("PASS: wrote P6.0 release evidence and report")


if __name__ == "__main__":
    main()
