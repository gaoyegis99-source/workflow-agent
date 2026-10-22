import runpy
from pathlib import Path

BUILD_REPORT = runpy.run_path(
    str(Path(__file__).resolve().parents[2] / "scripts/build_current_full_report.py")
)


def test_unhealthy_worker_blocks_current_report_gate():
    finding = BUILD_REPORT["unhealthy_worker_finding"]({"healthy": 16, "unhealthy": 1})

    assert finding["id"] == "WORKER_HEALTH_UNHEALTHY"
    assert finding["severity"] == "HIGH"
    assert finding["status"] == "OPEN"


def test_healthy_workers_do_not_add_unhealthy_finding():
    assert BUILD_REPORT["unhealthy_worker_finding"]({"healthy": 17}) is None
