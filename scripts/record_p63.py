# ruff: noqa: E501
"""Validate P6.3 gates and write durable release evidence."""

from __future__ import annotations

import hashlib
import json
import platform
import tomllib
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.8.3"
REQUIRED_CASES = {
    "test_catalog_and_context_are_deterministic_and_read_only",
    "test_deployment_catalog_round_trip_and_tamper_rejection",
    "test_namespaced_tool_contract_and_exact_arguments",
    "test_untrusted_observation_cannot_change_policy_or_register_tool",
    "test_context_omits_whole_optional_objects_and_never_truncates_requirements",
    "test_mandatory_context_overflow_is_explicit",
    "test_read_request_is_stable_replayable_and_wakes_next_frame",
    "test_p63_frame_rejects_write_proposal",
    "test_current_membership_is_rechecked_before_tool_execution",
    "test_model_read_then_explain_loop_has_no_business_action",
    "test_production_data_adapter_filters_list_and_enforces_exact_grant",
    "test_exact_evidence_read_uses_stable_agent_receipt",
    "test_production_worker_composes_frozen_context_model_and_local_read",
    "test_0012_to_0013_adds_read_ledger_and_is_repeatable",
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
    focused, all_cases = suite(verification / "p63-tests.xml")
    api, _ = suite(verification / "p63-api-tests.xml")
    clean(focused, 300, "P6.3 focused")
    clean(api, 2, "P6.3 HTTP contract")
    cases = [case for case in all_cases if case.get("classname", "").startswith("tests.p63")]
    names = {case["name"].split("[")[0] for case in cases}
    if not REQUIRED_CASES <= names:
        raise RuntimeError(f"Required P6.3 cases are missing: {sorted(REQUIRED_CASES - names)}")

    distribution = (verification / "distribution.log").read_text()
    for expected in (
        "four required wheels installed outside repository",
        "frozen P6.3 read tool catalogue",
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
        raise RuntimeError("All ten workspace packages must be frozen at 0.8.3")
    artifacts = sorted((release / "dist").glob("*.whl")) + sorted(
        (release / "dist").glob("*.tar.gz")
    )
    if len(artifacts) != 20 or any(VERSION not in artifact.name for artifact in artifacts):
        raise RuntimeError("The ten wheel and ten source distributions are incomplete")

    evidence = {
        "phase": "P6.3-context-and-read-only-agent",
        "version": VERSION,
        "status": "PASS",
        "finished_at": datetime.now(UTC).isoformat(),
        "commands": [
            "bash scripts/check_p6.sh --phase readonly",
            "bash scripts/check_p6.sh --phase all",
        ],
        "p63_tests": {"tests": len(cases), "failures": 0, "errors": 0, "skipped": 0},
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
            "platform": "0013_agent_read_tools",
            "data": "0004_document_assets",
            "execution": "0006_remote_reliability",
            "knowledge": "0002_lifecycle_outbox",
            "agent_checkpoint": "0001_langgraph_checkpoint",
        },
        "python": platform.python_version(),
        "required_cases": sorted(REQUIRED_CASES),
        "limits": [
            "Only the frozen P6.3 read catalogue is enabled; no execution, research, deliverable, acceptance, grant, budget, shell, Python or arbitrary HTTP write tool can be accepted.",
            "SearchHit remains discovery output. Only exact evidence reads create an EvidenceRef and stable read receipt.",
            "External text, Task prose, semantic metadata and service payloads remain typed UNTRUSTED_DATA and cannot alter policy or register tools.",
            "P6.3 can plan, explain, ask, wait and read, but cannot yet submit a scientific computation.",
        ],
    }
    (ROOT / "docs/verification/p63-read-only-agent-evidence.json").write_text(
        json.dumps(evidence, indent=2, ensure_ascii=False) + "\n"
    )
    report = f"""# P6.3 / 0.8.3 上下文与只读 Agent 报告

状态：**PASS**  
完成时间：{evidence["finished_at"]}

## 完成内容

- `ContextCompiler v1` 按固定政策、Task 控制事实、必要要求、只读目录和观察组装 ContextManifest v2；每部分记录摘要与 token 估算，省略整个可选对象并留下原因，必要要求放不下时明确返回 `CONTEXT_OVERFLOW`。
- 部署冻结 15 个只读 ToolSpec。模型只能引用固定 ID、版本和参数结构，不能注册工具、提交镜像、命令、URL 或任意代码。
- Platform `0013_agent_read_tools` 在服务读取前保存稳定 ToolRequest，服务读取后只保存一条不可变 ObservationReceipt。重放使用原请求和观察，不生成业务 Action。
- Data 资产列表按当前 Task 精确 grant 过滤；资产、Knowledge、Execution、Research 和 Acceptance 查询均重新检查当前成员、项目和 Task 身份。Knowledge 精确读取使用稳定回执，SearchHit 仍不是 EvidenceRef。
- 同一 AgentRun 现在可以连续处理多个 DecisionFrame；runtime 身份按 AgentRun 校验，不再把第二个 frame 误判为新的 runtime。
- 提供生产 `agent-worker` 组合入口和 `agent-tool-catalog generate/check` 管理命令。

## 测试结果

- P6.3 专项：{len(cases)} passed，0 failed，0 skipped。
- P6.3、P6.2、P6.1、P6.0、P2、契约和架构聚焦回归：{focused["tests"]} passed，0 failed，0 skipped，用时 {focused["seconds"]} 秒。
- HTTP/OpenAPI 定向回归：{api["tests"]} passed，0 failed，0 skipped。
- 十个 wheel 和十个 sdist 构建成功；仓库外安装、目录生成/校验、运行时 probe、checkpoint 管理和生产 Worker 入口通过。

## 范围与结论

P6.3 已打通“冻结上下文 → 模型提出读取 → 稳定 ToolRequest → 当前权限复核 → 精确服务读取 → ObservationReceipt → 下一帧解释”的闭环。资料正文与服务载荷不能提升为系统指令；WITHDRAWN 事实会原样呈现，不能被政策解释为当前支持证据。本轮没有开放任何科学计算或业务写入，因此任务不会因模型陈述而完成。按“不做过度兼容测试”的要求，本轮只运行受影响的 P6/P2/契约/架构门禁，没有重复 P4 长节点和远程故障矩阵。
"""
    (ROOT / "docs/verification/p63-read-only-agent-report.md").write_text(report)
    print("PASS: wrote P6.3 release evidence and report")


if __name__ == "__main__":
    main()
