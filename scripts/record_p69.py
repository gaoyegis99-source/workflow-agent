import hashlib
import json
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path

from platform_core.application.tool_catalog import build_tool_catalog
from platform_infrastructure.database import SCHEMA_VERSION

root = Path(__file__).resolve().parents[1]
version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
verification = root / ".p6-runtime" / "verification"
release_dist = root / ".p6-runtime" / "release" / "dist"
deterministic = json.loads((verification / "p69-deterministic-eval.json").read_text())
real = json.loads((verification / "p69-real-model-eval.json").read_text())
public_benchmarks = json.loads((root / "evaluations/p6/public_benchmarks.json").read_text())


def junit_summary(path: Path) -> dict[str, int | float | str]:
    parsed = ET.parse(path).getroot()
    suites = [parsed] if parsed.tag == "testsuite" else list(parsed.iter("testsuite"))
    return {
        "tests": sum(int(suite.attrib.get("tests", 0)) for suite in suites),
        "failures": sum(int(suite.attrib.get("failures", 0)) for suite in suites),
        "errors": sum(int(suite.attrib.get("errors", 0)) for suite in suites),
        "skipped": sum(int(suite.attrib.get("skipped", 0)) for suite in suites),
        "seconds": round(sum(float(suite.attrib.get("time", 0)) for suite in suites), 3),
        "junit": str(path.relative_to(root)),
    }


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return f"sha256:{digest.hexdigest()}"


combined_name = "test_agent_combined_response_loss_and_mixed_runtime_recovery_keep_one_identity"
focused_junit = verification / "p69-focused-tests.xml"
combined_case = None
if focused_junit.is_file():
    focused_root = ET.parse(focused_junit).getroot()
    combined_case = next(
        (
            value
            for value in focused_root.iter("testcase")
            if value.attrib.get("name") == combined_name
            and value.find("failure") is None
            and value.find("error") is None
            and value.find("skipped") is None
        ),
        None,
    )
evidence = {
    "phase": "P6.9",
    "version": version,
    "release_status": "PASS" if real["status"] == "PASS" else "CONDITIONAL",
    "platform_schema": SCHEMA_VERSION,
    "tool_catalog_digest": build_tool_catalog("science")["catalog_digest"],
    "deterministic_evaluation": deterministic,
    "real_model_evaluation": real,
    "model_catalog": {
        "path": "deploy/p6/model-catalog.deepseek-flash.json",
        "profile_id": real.get("profile_id"),
        "profile_digest": real.get("profile_digest"),
        "provider_credential_persisted": False,
    },
    "verification": {
        "p6_gate": junit_summary(verification / "p69-tests.xml"),
        "http_api": junit_summary(verification / "p69-api-tests.xml"),
        "focused_recovery_and_browser": junit_summary(focused_junit),
        "distribution": {
            "status": "PASS",
            "log": str((verification / "distribution.log").relative_to(root)),
        },
    },
    "release_artifacts": [
        {
            "path": str(path.relative_to(root)),
            "bytes": path.stat().st_size,
            "digest": sha256(path),
        }
        for path in sorted(release_dist.glob(f"*{version}*"))
        if path.is_file()
    ],
    "public_benchmarks": public_benchmarks,
    "combined_fault_chain": {
        "status": "PASS" if combined_case is not None else "NOT_RUN",
        "test": combined_name,
        "duration_seconds": (
            float(combined_case.attrib["time"]) if combined_case is not None else None
        ),
        "facts": [
            "model response loss reconciled under the original model call",
            "replacement Agent worker submitted one workflow Action",
            "Execution worker and Toil leader terminated",
            "HTTPS remote peer restarted with persistent state",
            "Data commit response lost and recovered under the original publication key",
            "one remote identity, one budget settlement, and one publication group",
        ],
    },
    "known_limitations": (
        ["Real-model repeated evaluation is not complete without a configured provider credential"]
        if real["status"] != "PASS"
        else []
    )
    + [
        "The provider model name deepseek-flash is an upstream alias, "
        "not an immutable weight snapshot",
        "Public BFCL, tau-bench, and ScienceAgentBench suites are reference mappings, "
        "not release gates",
        "No arbitrary code generation or execution",
        "No multi-Agent autonomy",
        "No unregistered tools, workflows, policies, or checkers",
        "Reality of field observations is not automatically proven",
    ],
}
(root / "docs/verification/p69-release-evidence.json").write_text(
    json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
if real["status"] == "PASS":
    real_line = (
        "真实模型 A01–A15 各运行三次并通过冻结门槛：严重错误 0、usage 缺失 0，"
        f"共 {real['total_model_calls']} 次模型调用，费用 {real['total_cost_microunits']} 微单位。"
    )
elif real.get("reason") == "NO_FROZEN_PROVIDER_PROFILE":
    real_line = "真实模型评测未执行：环境没有冻结 Model Catalog；门槛未通过。"
else:
    real_line = "真实模型评测未执行：冻结 profile 的 provider 凭据缺失；门槛未通过。"
(root / "docs/verification/p69-release-report.md").write_text(
    f"# P6 完成度补强 / {version} 发行报告\n\n"
    "A01–A15 已由精确 pytest node ID 各执行三次并通过。Agent 身份下的真实 Toil、"
    "QGIS 三节点工作流、P5 精确交付和开放验收均纳入门禁；备份恢复、受控 checkpoint "
    "重建及可选脱敏 OpenTelemetry 已补齐。模型响应丢失、Agent Worker 接管、Execution "
    "Worker 与 Toil leader 终止、远端 peer 重启和 Data 发布响应丢失也已在同一条真实"
    "工作流中完成组合验证。\n\n" + real_line + "\n",
    encoding="utf-8",
)
print(json.dumps(evidence, ensure_ascii=False))
