import json
from pathlib import Path

from platform_core.application.tool_catalog import build_tool_catalog
from platform_infrastructure.database import SCHEMA_VERSION

root = Path(__file__).resolve().parents[1]
verification = root / ".p6-runtime" / "verification"
catalog = build_tool_catalog("science")
evidence = {
    "phase": "P6.6",
    "version": "0.8.6",
    "platform_schema": SCHEMA_VERSION,
    "tool_catalog_digest": catalog["catalog_digest"],
    "capabilities": [
        "frozen_workflow_submission",
        "analysis_revision",
        "analysis_registration",
        "proposed_claim",
        "exact_knowledge_evidence_reads",
        "policy_decision_reuse",
    ],
    "tests": {
        "stage": str(verification / "p66-tests.xml"),
        "http": str(verification / "p66-api-tests.xml"),
    },
}
(root / "docs/verification/p66-science-agent-evidence.json").write_text(
    json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
(root / "docs/verification/p66-science-agent-report.md").write_text(
    "# P6.6 / 0.8.6 阶段报告\n\n"
    "Agent 可调用用户冻结的工作流、分析协议、登记和拟议主张。"
    "所有科学记录仍由 P4/P5 用例校验和保存；"
    "Knowledge 文本不能定义工具或可执行策略。\n",
    encoding="utf-8",
)
print(json.dumps(evidence, ensure_ascii=False))
