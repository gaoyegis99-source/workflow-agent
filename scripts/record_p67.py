import json
from pathlib import Path

from platform_infrastructure.database import SCHEMA_VERSION

root = Path(__file__).resolve().parents[1]
verification = root / ".p6-runtime" / "verification"
evidence = {
    "phase": "P6.7",
    "version": "0.8.7",
    "platform_schema": SCHEMA_VERSION,
    "repair_rules": {
        "same_failure_limit": 2,
        "unknown": "RECONCILE_ORIGINAL_IDENTITY",
        "acceptance_error": "EXPLICIT_RETRY_OF",
        "hidden_checker_answers_exposed": False,
    },
    "tests": {
        "stage": str(verification / "p67-tests.xml"),
        "http": str(verification / "p67-api-tests.xml"),
    },
}
(root / "docs/verification/p67-bounded-repair-evidence.json").write_text(
    json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
(root / "docs/verification/p67-bounded-repair-report.md").write_text(
    "# P6.7 / 0.8.7 阶段报告\n\n"
    "每个受控工具分支保存不可变尝试链。相同确定性失败最多两次；"
    "外部未知只能按原身份对账；达到上限后 Agent 明确 BLOCKED。\n",
    encoding="utf-8",
)
print(json.dumps(evidence, ensure_ascii=False))
