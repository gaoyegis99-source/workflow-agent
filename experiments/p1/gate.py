"""Explicit P1 selection; missing checks can never qualify a candidate."""

REQUIRED = {
    "Toil": {
        "submit_query_relocate_duplicate_scatter",
        "failed_attempt_restart_same_jobstore",
        "cancel_live_worker_and_repeat",
    },
    "Weaver": {
        "submit_query_output",
        "nested_duplicate_scatter_identity",
        "api_restart_relocates_job",
        "cancel_stops_actual_tool",
    },
    "gVisor": {"guest_files_network_and_tmp_quota", "guest_cancel_and_container_relocate"},
}


def qualified(name: str, report: dict) -> bool:
    if report.get("status") != "PASS":
        return False
    checks = report.get("checks", [])
    names = [item["check"] for item in checks]
    return (
        len(names) == len(set(names))
        and set(names) == REQUIRED[name]
        and all(item.get("status") == "PASS" for item in checks)
    )


def select_engine(toil: dict, weaver: dict, isolation: dict) -> str:
    if not qualified("gVisor", isolation):
        raise ValueError("P1 isolation gate failed; remain in P1")
    if qualified("Toil", toil):
        return "Toil"
    if qualified("Weaver", weaver):
        return "Weaver"
    raise ValueError("Neither CWL candidate qualified; workflow integration is blocked")
