# ruff: noqa: E501
"""Validate P6.1 gates and write durable release evidence."""

from __future__ import annotations

import hashlib
import json
import platform
import tomllib
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.8.1"
REQUIRED_CASES = {
    "test_agent_task_is_test_enabled_only_and_creates_one_ledger",
    "test_accepted_proposal_response_loss_replays_without_a_second_plan",
    "test_two_workers_compete_for_one_lease_and_expired_lease_is_taken_over",
    "test_old_generation_and_task_revision_cannot_write",
    "test_question_answer_and_revision_order_are_explicit",
    "test_duplicate_out_of_order_and_conflicting_events",
    "test_pause_resume_and_cancel_fence_agent_progress",
    "test_checkpoint_ahead_is_discarded_without_accepting_unledgered_proposal",
    "test_checkpoint_unavailable_keeps_epoch_and_retries_same_runtime",
    "test_runtime_can_continue_after_ledger_recovery",
    "test_0010_to_0011_preserves_direct_task_and_is_repeatable",
    "test_checkpoint_database_requires_explicit_repeatable_migration",
}


def file_digest(path: Path) -> str:
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
    if result["tests"] < minimum or any(result[key] for key in ("failures", "errors", "skipped")):
        raise RuntimeError(f"{label} gate is incomplete: {result}")


def main() -> None:
    verification = ROOT / ".p6-runtime" / "verification"
    release = ROOT / ".p6-runtime" / "release"
    focused, focused_cases = suite(verification / "p61-tests.xml")
    full, _ = suite(verification / "all-tests.xml")
    require_clean(focused, 180, "P6.1 focused")
    require_clean(full, 698, "full regression")
    p61_cases = [
        case for case in focused_cases if case.get("classname", "").startswith("tests.p61")
    ]
    names = {case["name"].split("[")[0] for case in p61_cases}
    if len(p61_cases) < 18 or not REQUIRED_CASES <= names:
        raise RuntimeError("Required P6.1 lease, event, recovery or migration cases are missing")

    distribution = (verification / "distribution.log").read_text()
    for expected in (
        "four required wheels installed outside repository",
        "one-decision replay semantics",
        "explicit checkpoint migrate/check-schema administration",
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
        raise RuntimeError("All ten workspace packages must be frozen at 0.8.1")

    artifacts = sorted((release / "dist").glob("*.whl")) + sorted(
        (release / "dist").glob("*.tar.gz")
    )
    if len(artifacts) != 20 or any(VERSION not in artifact.name for artifact in artifacts):
        raise RuntimeError("The ten wheel and ten source distributions are incomplete")
    profiles = json.loads((verification / "runtime-profiles.json").read_text())

    evidence = {
        "phase": "P6.1-deterministic-agent-kernel",
        "version": VERSION,
        "status": "PASS",
        "finished_at": datetime.now(UTC).isoformat(),
        "commands": [
            "bash scripts/check_p6.sh --phase kernel",
            "bash scripts/check_p6.sh --phase all",
        ],
        "p61_tests": {"tests": len(p61_cases), "failures": 0, "errors": 0, "skipped": 0},
        "focused_regression": focused,
        "full_regression": full,
        "packages": packages,
        "runtime_profiles": profiles,
        "release_artifacts": {
            artifact.name: {"bytes": artifact.stat().st_size, "sha256": file_digest(artifact)}
            for artifact in artifacts
        },
        "lock_digest": file_digest(ROOT / "uv.lock"),
        "schemas": {
            "platform": "0011_agent_coordination",
            "data": "0004_document_assets",
            "execution": "0006_remote_reliability",
            "knowledge": "0002_lifecycle_outbox",
            "agent_checkpoint": "0001_langgraph_checkpoint",
        },
        "python": platform.python_version(),
        "required_cases": sorted(REQUIRED_CASES),
        "limits": [
            "AGENT task creation is enabled only by an injected test deployment profile; the default production API still rejects it.",
            "The kernel uses scripted decisions only and cannot execute business tools or call a real model provider.",
            "Checkpoint storage is operational state; the Platform ledger remains authoritative and recovery always creates a new epoch.",
            "P4 long performance experiments were not repeated because execution scheduling did not change.",
        ],
    }
    (ROOT / "docs/verification/p61-agent-kernel-evidence.json").write_text(
        json.dumps(evidence, indent=2, ensure_ascii=False) + "\n"
    )
    report = f"""# P6.1 / 0.8.1 确定性 Agent 内核报告

状态：**PASS**  
完成时间：{evidence["finished_at"]}

## 完成内容

- Platform `0011_agent_coordination` 保存 AgentRun、不可变计划修订、ContextManifest、DecisionFrame、Proposal、问题/回答、事件、wakeup、outbox 和带代次租约。
- Agent Worker 每轮只领取一个租约、冻结一个 frame、取得一个结构化 proposal 并回写 ledger；运行时不能直接调用业务仓储或执行工具。
- Task 修订、暂停、恢复、取消和外部事件都会提升 generation、使旧 frame 失效并阻止旧 Worker 回写。
- LangGraph checkpoint 使用独立 PostgreSQL 数据库和显式 `0001_langgraph_checkpoint` 管理命令；缺失、损坏、落后或超前时从 Platform ledger 建立新 epoch，不接受 checkpoint 中未入账的 proposal。
- 默认 API 仍拒绝 AGENT Task；只有显式注入冻结 profile 的测试部署可以创建，用于本阶段内核验收。

## 测试结果

- P6.1 专项：{len(p61_cases)} passed，0 failed，0 skipped。
- P6.1、P6.0、P2、契约和架构聚焦回归：{focused["tests"]} passed，0 failed，0 skipped，用时 {focused["seconds"]} 秒。
- 完整回归：{full["tests"]} passed，0 failed，0 skipped，用时 {full["seconds"]} 秒。
- 十个 wheel、十个 sdist 构建成功；仓库外安装、依赖检查、运行时 probe、checkpoint 管理 CLI 和两个 adapter 重放语义通过。

## 已关闭问题

- 回答事实中的数据库时间与 JSON 幂等回执分离，重复回答返回同一回执。
- 已接受 proposal 在响应丢失后可按同一 frame 和摘要重放，不会再次创建计划或副作用。
- 同一事件 ID 内容冲突、旧来源 revision、旧租约 generation 和旧 TaskSpec revision 均不能覆盖权威状态。

## 范围与结论

P6.1 已形成可恢复的确定性协调内核，但没有真实模型、模型费用、Data/Knowledge 工具或计算提交。生产入口继续拒绝 AGENT Task。P6.1 通过并结束；下一轮是 P6.2 模型网关和预算，本轮未实施。
"""
    (ROOT / "docs/verification/p61-agent-kernel-report.md").write_text(report)
    print("PASS: wrote P6.1 release evidence and report")


if __name__ == "__main__":
    main()
