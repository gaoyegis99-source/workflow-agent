import json
from pathlib import Path

from platform_infrastructure.database import SCHEMA_VERSION

root = Path(__file__).resolve().parents[1]
verification = root / ".p6-runtime" / "verification"
evidence = {
    "phase": "P6.8",
    "version": "0.8.8",
    "platform_schema": SCHEMA_VERSION,
    "ui": [
        "agent_status",
        "typed_questions",
        "decision_and_repair_timeline",
        "model_usage",
        "resumable_sse_cursor",
    ],
    "operations": ["list-waiting", "list-blocked", "reconcile", "export"],
    "forbidden_operations": ["force-pass", "write-result", "direct-database-update"],
    "tests": {
        "stage": str(verification / "p68-tests.xml"),
        "http": str(verification / "p68-api-tests.xml"),
    },
}
(root / "docs/verification/p68-agent-ui-evidence.json").write_text(
    json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
(root / "docs/verification/p68-agent-ui-report.md").write_text(
    "# P6.8 / 0.8.8 阶段报告\n\n"
    "任务页展示 Agent 状态、问题、决策、等待、修复和模型用量。"
    "带 Bearer Token 的可恢复 SSE 使用事件游标；浏览器断线不影响 Worker。"
    "运维 CLI 仅提供查询、导出和正常对账。\n",
    encoding="utf-8",
)
print(json.dumps(evidence, ensure_ascii=False))
