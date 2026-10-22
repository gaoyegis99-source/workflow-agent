# ruff: noqa: E501
"""Validate P6.2 gates and write durable release evidence."""

from __future__ import annotations

import hashlib
import json
import platform
import tomllib
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.8.2"
REQUIRED_CASES = {
    "test_0011_to_0012_preserves_agent_ledger_and_is_repeatable",
    "test_catalog_is_deterministic_and_platform_worker_load_same_digest",
    "test_runtime_profile_file_is_identical_for_platform_and_worker",
    "test_completed_model_call_is_reserved_settled_and_accepted_once",
    "test_response_loss_and_worker_takeover_query_original_call",
    "test_rate_limit_retries_same_model_call_and_reservation",
    "test_completed_response_with_missing_usage_is_visible_and_conservative",
    "test_one_format_repair_is_a_distinct_budgeted_call",
    "test_second_invalid_output_blocks_without_a_third_call",
    "test_same_frame_concurrency_creates_one_call_and_one_reservation",
    "test_old_worker_cannot_write_model_observation_after_takeover",
    "test_wall_clock_budget_blocks_a_new_model_call",
    "test_openai_responses_adapter_uses_real_http_and_stable_idempotency_key",
    "test_provider_ref_temporarily_not_visible_keeps_original_identity",
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


def require_clean(result, minimum, label):
    if result["tests"] < minimum or any(result[key] for key in ("failures", "errors", "skipped")):
        raise RuntimeError(f"{label} gate is incomplete: {result}")


def main():
    verification = ROOT / ".p6-runtime/verification"
    release = ROOT / ".p6-runtime/release"
    focused, focused_cases = suite(verification / "p62-tests.xml")
    api, _ = suite(verification / "p62-api-tests.xml")
    require_clean(focused, 290, "P6.2 focused")
    require_clean(api, 2, "P6.2 HTTP contract")
    cases = [case for case in focused_cases if case.get("classname", "").startswith("tests.p62")]
    names = {case["name"].split("[")[0] for case in cases}
    if len(cases) < 25 or not REQUIRED_CASES <= names:
        raise RuntimeError("Required P6.2 model, budget, recovery or migration cases are missing")

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
        raise RuntimeError("All ten workspace packages must be frozen at 0.8.2")

    artifacts = sorted((release / "dist").glob("*.whl")) + sorted(
        (release / "dist").glob("*.tar.gz")
    )
    if len(artifacts) != 20 or any(VERSION not in artifact.name for artifact in artifacts):
        raise RuntimeError("The ten wheel and ten source distributions are incomplete")

    evidence = {
        "phase": "P6.2-budgeted-model-gateway",
        "version": VERSION,
        "status": "PASS",
        "finished_at": datetime.now(UTC).isoformat(),
        "commands": [
            "bash scripts/check_p6.sh --phase model",
            "bash scripts/check_p6.sh --phase all",
        ],
        "p62_tests": {"tests": len(cases), "failures": 0, "errors": 0, "skipped": 0},
        "focused_regression": focused,
        "http_contract_regression": api,
        "historical_full_regression": {
            "baseline": "P6.1 / 0.8.1",
            "tests": 698,
            "current_run": "not repeated by user direction; 532 historical tests completed with zero failures before the redundant long P4.3.2 matrix was stopped",
        },
        "packages": packages,
        "runtime_profiles": json.loads((verification / "runtime-profiles.json").read_text()),
        "release_artifacts": {
            artifact.name: {"bytes": artifact.stat().st_size, "sha256": file_digest(artifact)}
            for artifact in artifacts
        },
        "lock_digest": file_digest(ROOT / "uv.lock"),
        "schemas": {
            "platform": "0012_agent_model_calls",
            "data": "0004_document_assets",
            "execution": "0006_remote_reliability",
            "knowledge": "0002_lifecycle_outbox",
            "agent_checkpoint": "0001_langgraph_checkpoint",
        },
        "python": platform.python_version(),
        "required_cases": sorted(REQUIRED_CASES),
        "limits": [
            "P6.2 context contains TaskSpec, current plan and a test-only tool catalogue; Data and Knowledge content are not exposed yet.",
            "P6.2 can produce ledger proposals but cannot execute business tools or scientific actions.",
            "The production provider requires an explicit frozen catalogue, runtime profile and environment-provided credential.",
            "Provider usage that is missing or uncertain remains visibly reserved until reconciliation.",
        ],
    }
    (ROOT / "docs/verification/p62-model-gateway-evidence.json").write_text(
        json.dumps(evidence, indent=2, ensure_ascii=False) + "\n"
    )
    report = f"""# P6.2 / 0.8.2 模型网关与预算报告

状态：**PASS**  
完成时间：{evidence["finished_at"]}

## 完成内容

- Platform `0012_agent_model_calls` 保存模型额度、逐调用预留、冻结请求、供应商观察和恢复对账材料；模型 I/O 前已在同一事务完成预算准入。
- 部署使用摘要冻结的 runtime profile 和 model catalogue。客户端不能指定供应商、模型、endpoint、实现、价格或凭据；凭据只从 Agent Worker 环境读取。
- Agent Worker 支持真实 Responses HTTP 适配、确定性 fake 和 record/replay。每个 ModelCall ID 同时作为稳定供应商幂等键，响应丢失、429、5xx、超时和临时 404 均查询或重提原身份。
- structured output 经过严格判别联合校验。首次格式错误最多触发一个独立、计费且有预算预留的 JSON 修复调用；再次错误使 AgentRun 明确阻塞。
- API 提供只读 model usage，分别显示使用、预留、结果未知和 usage 未知；墙钟、决策轮数、调用、token、reasoning 和估算费用均有上限。

## 测试结果

- P6.2 专项：{len(cases)} passed，0 failed，0 skipped。
- P6.2、P6.1、P6.0、P2、契约和架构聚焦回归：{focused["tests"]} passed，0 failed，0 skipped，用时 {focused["seconds"]} 秒。
- 当前变更的 HTTP/OpenAPI 回归：{api["tests"]} passed，0 failed，0 skipped，用时 {api["seconds"]} 秒。
- 十个 wheel、十个 sdist 构建成功；仓库外安装、运行时 probe、checkpoint 管理 CLI 和发行依赖检查通过。

## 已关闭问题

- 429 后的同一供应商请求保留 ModelCall ID，同时让本地观察 revision 单调递增，后续成功不会与限流事实冲突。
- Worker 接管、供应商响应丢失和 provider ref 暂不可见时不会创建第二个调用、第二份预留或第二个 proposal。
- usage 缺失不会被当作零费用；调用次数已结算，token 和费用预留继续保留并在 API 中显示未知。
- Platform 与 Agent Worker 对运行时和模型目录做相同摘要核验；单边配置使 readiness 不通过。

## 范围与结论

P6.2 已打通“冻结上下文 → 预算预留 → 真实模型协议 → 严格 proposal → ledger 接受”的模型决策链。它尚不读取 Data/Knowledge，也不能提交业务工具或科学计算。根据本轮“不做过度兼容测试”的要求，没有重新跑完与本次变更无关的 P4.3.2 长故障矩阵；P6.1 的 698 项完整基线保留，本轮全量尝试停止前已有 532 项零失败。P6.2 通过并结束；下一轮应进入 P6.3 上下文编译和只读工具，本轮未提前实现。
"""
    (ROOT / "docs/verification/p62-model-gateway-report.md").write_text(report)
    print("PASS: wrote P6.2 release evidence and report")


if __name__ == "__main__":
    main()
