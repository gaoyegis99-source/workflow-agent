# ruff: noqa: E501
"""Validate P6.4 gates and write durable release evidence."""

from __future__ import annotations

import hashlib
import json
import platform
import tomllib
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.8.4"
REQUIRED_CASES = {
    "test_action_catalog_enables_only_controlled_stage",
    "test_action_request_is_linked_once_and_replay_does_not_repeat",
    "test_action_context_rejects_read_tool_in_action_proposal",
    "test_production_action_uses_current_task_contract_and_agent_identity",
    "test_action_membership_is_rechecked_before_backend_call",
    "test_action_is_rejected_after_task_revision_changes",
    "test_p64_schema_has_action_links_and_nullable_agent_token",
    "test_agent_tools_complete_real_p3_closed_loop",
    "test_deterministic_agent_runs_p3_to_completion_and_explains",
}


def file_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def suite(path: Path):
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


def clean(result, minimum, label):
    if result["tests"] < minimum or any(result[key] for key in ("failures", "errors", "skipped")):
        raise RuntimeError(f"{label} gate is incomplete: {result}")


def main():
    verification = ROOT / ".p6-runtime/verification"
    release = ROOT / ".p6-runtime/release"
    focused, all_cases = suite(verification / "p64-tests.xml")
    api, _ = suite(verification / "p64-api-tests.xml")
    clean(focused, 300, "P6.4 focused")
    clean(api, 2, "P6.4 HTTP contract")
    cases = [case for case in all_cases if case.get("classname", "").startswith("tests.p64")]
    names = {case["name"].split("[")[0] for case in cases}
    if not REQUIRED_CASES <= names:
        raise RuntimeError(f"Required P6.4 cases are missing: {sorted(REQUIRED_CASES - names)}")

    distribution = (verification / "distribution.log").read_text()
    for expected in (
        "four required wheels installed outside repository",
        "frozen P6.4 action tool catalogue",
        "production Agent worker composition root",
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
        raise RuntimeError("All ten workspace packages must be frozen at 0.8.4")
    artifacts = sorted((release / "dist").glob("*.whl")) + sorted(
        (release / "dist").glob("*.tar.gz")
    )
    if len(artifacts) != 20 or any(VERSION not in artifact.name for artifact in artifacts):
        raise RuntimeError("The ten wheel and ten source distributions are incomplete")

    evidence = {
        "phase": "P6.4-controlled-operator-loop",
        "version": VERSION,
        "status": "PASS",
        "finished_at": datetime.now(UTC).isoformat(),
        "commands": [
            "bash scripts/check_p6.sh --phase actions",
            "bash scripts/check_p6.sh --phase all",
        ],
        "p64_tests": {"tests": len(cases), "failures": 0, "errors": 0, "skipped": 0},
        "focused_regression": focused,
        "http_contract_regression": api,
        "packages": packages,
        "runtime_profiles": json.loads((verification / "runtime-profiles.json").read_text()),
        "release_artifacts": {
            artifact.name: {"bytes": artifact.stat().st_size, "sha256": file_digest(artifact)}
            for artifact in artifacts
        },
        "lock_digest": file_digest(ROOT / "uv.lock"),
        "tool_catalog_digest": file_digest(ROOT / ".p6-runtime/agent-tool-catalog.json"),
        "schemas": {
            "platform": "0014_agent_action_links",
            "data": "0004_document_assets",
            "execution": "0006_remote_reliability",
            "knowledge": "0002_lifecycle_outbox",
            "agent_checkpoint": "0001_langgraph_checkpoint",
        },
        "python": platform.python_version(),
        "required_cases": sorted(REQUIRED_CASES),
        "limits": [
            "P6.4 write access is limited to two existing P3 operators, legacy P3 deliverable registration, and the existing P3 independent acceptance use case.",
            "The model cannot supply implementation IDs, images, commands, arbitrary parameters, grants, budgets, checker results, or completion facts.",
            "Business transitions are projected from committed Platform facts; P6.5 will broaden dependency watermarks and long-wait recovery beyond this first closed loop.",
            "No P4 long-node performance or remote fault matrix was repeated for this logic-focused Agent stage.",
        ],
    }
    (ROOT / "docs/verification/p64-controlled-agent-evidence.json").write_text(
        json.dumps(evidence, indent=2, ensure_ascii=False) + "\n"
    )
    report = f"""# P6.4 / 0.8.4 受控单算子 Agent 闭环报告

状态：**PASS**  
完成时间：{evidence["finished_at"]}

## 完成内容

- 冻结 P6.4 工具目录在原 15 个只读工具上新增三个受控写工具：`execution.submit_operator`、`deliverable.register`、`acceptance.request`。
- 算子提交只允许 P3 的完整率与描述性线性趋势。服务端从当前 TaskSpec 取得精确输入、参数和验收范围，从受信目录取得实现，模型不能提交镜像、命令、地址或任意参数。
- Agent continuation 不保存用户 Bearer Token；每次工具准入、outbox 发送和 Execution permit 都按发起主体重新检查当前项目角色、任务修订、grant 与预算。
- Platform `0014_agent_action_links` 保存 ToolRequest 到 Action、Command、Deliverable 或 Acceptance 的稳定关系。同一请求重放不会重复计算、扣账或登记成果。
- Action 状态投影器把已提交、运行、发布与验收事实转换为有版本的 Agent 事件。Acceptance PASS 后允许最后一个只读终态帧，模型只能提交 `EXPLANATION`。
- 真实 P3 容器闭环已经打通：两个算子各执行一次，整组成果登记一次，独立 Decimal 检查一次，预算最终为 used=3、reserved=0、in_flight=0，Task 转为 COMPLETED。

## 测试结果

- P6.4 专项：{len(cases)} passed，0 failed，0 skipped。
- P6.4 至 P6.0、P2、契约和架构聚焦回归：{focused["tests"]} passed，0 failed，0 skipped，用时 {focused["seconds"]} 秒。
- HTTP/OpenAPI 定向回归：{api["tests"]} passed，0 failed，0 skipped。
- 十个 wheel 和十个 sdist 构建成功；仓库外安装、Action 工具目录、运行时、checkpoint 与生产 Worker 组合入口通过。

## 范围与结论

P6.4 已形成“模型提出受控计算 → 既有 P3 Action/预算/授权 → 真实容器与发布 → 交付物 → 独立验收 → Task 完成 → 最终解释”的第一条自主业务闭环。科学结果和完成事实仍由 Execution、Data 与 Acceptance 掌握，模型只能提出有类型的请求。本轮按要求聚焦逻辑，没有重复 P4 长性能与远程组合矩阵。下一轮 P6.5 负责更通用的长等待、依赖水位、服务事件和进程恢复。
"""
    (ROOT / "docs/verification/p64-controlled-agent-report.md").write_text(report)
    print("PASS: wrote P6.4 release evidence and report")


if __name__ == "__main__":
    main()
