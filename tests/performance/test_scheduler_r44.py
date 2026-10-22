import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.p4.scheduler_r44 import assess_trigger  # noqa: E402, I001


PROFILE = {"nodes": 8, "repeats": 1}


def run(identity, queue, starvation=0, posts=16, warnings=0):
    return {
        "configuration": {"id": identity},
        "concurrent_seconds": 90,
        "node_queue_ms": {"p95": queue},
        "lease_sampling": {"parent_saturation_with_ready_nodes_samples": starvation},
        "node_submit_posts": posts,
        "process_group_warnings": warnings,
    }


def test_r44_requires_all_three_trigger_facts():
    result = assess_trigger(
        [run("TWO_WORKERS", 900, starvation=0), run("FOUR_WORKERS", 300)], PROFILE
    )
    assert result["decision"] == "CANCEL_R44"
    assert result["trigger_checks"] == {
        "two_worker_queue_p95_exceeds_500ms": True,
        "parents_saturate_workers_while_nodes_wait": False,
        "ordinary_workers_materially_reduce_queue": True,
    }


def test_r44_opens_only_for_parent_starvation_improved_by_workers():
    result = assess_trigger(
        [run("TWO_WORKERS", 900, starvation=3), run("FOUR_WORKERS", 300)], PROFILE
    )
    assert result["decision"] == "IMPLEMENT_WORK_CLASS"
    assert result["integrity_failures"] == []


def test_r44_reports_duplicate_submission_or_lifecycle_warning():
    result = assess_trigger(
        [
            run("TWO_WORKERS", 900, starvation=3, posts=17),
            run("FOUR_WORKERS", 300, warnings=1),
        ],
        PROFILE,
    )
    assert len(result["integrity_failures"]) == 2
