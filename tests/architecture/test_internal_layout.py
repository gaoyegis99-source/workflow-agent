from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[2]
CHECK = SOURCE_ROOT / "scripts" / "maintenance" / "check_internal_layout.py"


def test_internal_layout_contract_passes() -> None:
    result = subprocess.run(
        [sys.executable, str(CHECK)],
        cwd=SOURCE_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "INTERNAL_LAYOUT_CHECK_OK" in result.stdout


def test_p8_layout_is_project_owned_without_a_release_identity_gate() -> None:
    source = (SOURCE_ROOT / "scripts/lib/runtime_layout.py").read_text(encoding="utf-8")
    shell = (SOURCE_ROOT / "scripts/lib/runtime-layout.sh").read_text(encoding="utf-8")
    assert "require_resolved_p8_release_identity" not in source
    assert "require_resolved_p8_release_identity" not in shell
    assert "runtime/p8" in source
    layout = json.loads((SOURCE_ROOT.parent / "project-layout.json").read_text())
    assert layout["runtime"]["p8"] == "runtime/p8"
    assert "runtime_status" not in layout
