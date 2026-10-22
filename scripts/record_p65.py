import json
from pathlib import Path

from platform_infrastructure.database import SCHEMA_VERSION

root = Path(__file__).resolve().parents[1]
verification = root / ".p6-runtime" / "verification"
evidence = {
    "phase": "P6.5",
    "version": "0.8.5",
    "platform_schema": SCHEMA_VERSION,
    "capabilities": [
        "persistent_wait_dependencies",
        "source_event_watermarks",
        "lost_event_reconciliation",
        "runtime_epoch_recovery_evidence",
        "intermediate_state_model_suppression",
    ],
    "tests": {
        "stage": str(verification / "p65-tests.xml"),
        "http": str(verification / "p65-api-tests.xml"),
    },
    "excluded": ["P4 performance matrices", "P6.6 scientific planning"],
}
target = root / "docs" / "verification" / "p65-wakeup-recovery-evidence.json"
target.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
report = root / "docs" / "verification" / "p65-wakeup-recovery-report.md"
report.write_text(
    "# P6.5 / 0.8.5 阶段报告\n\n"
    "实现冻结等待依赖、来源水位、丢失事件对账和运行时 epoch 恢复记录。"
    "中间业务状态继续入账，但不会反复唤醒模型。\n\n"
    "统一门禁：`bash scripts/check_p6.sh --phase recovery` 与 "
    "`bash scripts/check_p6.sh --phase all`。\n",
    encoding="utf-8",
)
print(json.dumps(evidence, ensure_ascii=False))
