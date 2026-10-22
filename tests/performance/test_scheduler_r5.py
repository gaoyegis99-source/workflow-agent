import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.p4 import scheduler_r5  # noqa: E402
from benchmarks.p4.scheduler_r5 import selection_decision  # noqa: E402


def decide(*, total_gain=10, compute_gain=0, safety=True, on_gate=True, regression=True):
    return selection_decision(
        standard=True,
        safety_qualified=safety,
        integrity_failures=[],
        off_absolute={"gate": True},
        on_absolute={"gate": on_gate},
        regressions={"metric": regression},
        total_gain=total_gain,
        compute_gain=compute_gain,
    )


def test_r5_selects_on_only_after_ten_percent_gain_and_all_gates():
    assert decide() == "SELECT_CHAINING_ON"
    assert decide(total_gain=0, compute_gain=10) == "SELECT_CHAINING_ON"


def test_r5_keeps_off_without_material_gain():
    assert decide(total_gain=9.999, compute_gain=9.999) == "KEEP_CHAINING_OFF"


def test_r5_keeps_off_when_safety_absolute_or_regression_gate_fails():
    assert decide(safety=False) == "KEEP_CHAINING_OFF"
    assert decide(on_gate=False) == "KEEP_CHAINING_OFF"
    assert decide(regression=False) == "KEEP_CHAINING_OFF"


def test_r5_safety_qualification_requires_complete_passing_report(tmp_path, monkeypatch):
    report = tmp_path / "safety.xml"
    cases = "".join(f'<testcase name="{name}"/>' for name in scheduler_r5.SAFETY_TESTS)
    report.write_text(f'<testsuite tests="5">{cases}</testsuite>')
    monkeypatch.setattr(scheduler_r5, "SAFETY_REPORT", report)
    result = scheduler_r5.safety_qualification("standard")
    assert result["status"] == "PASS"
    assert result["tests"] == 5


def test_r5_safety_qualification_rejects_failed_or_missing_report(tmp_path, monkeypatch):
    report = tmp_path / "safety.xml"
    report.write_text(
        '<testsuite tests="1"><testcase name="wrong"><failure/></testcase></testsuite>'
    )
    monkeypatch.setattr(scheduler_r5, "SAFETY_REPORT", report)
    assert scheduler_r5.safety_qualification("standard")["status"] == "FAIL"
    monkeypatch.setattr(scheduler_r5, "SAFETY_REPORT", tmp_path / "missing.xml")
    assert scheduler_r5.safety_qualification("standard")["status"] == "NOT_RUN"
