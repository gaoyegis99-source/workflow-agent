"""Execution facts are monotone; delivery failures are separate recovery metadata."""

TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "REJECTED", "SEALED"})
STATES = TERMINAL | {"QUEUED", "RUNNING"}


def validate_change(previous, changes):
    state = changes.get("state", previous["state"])
    executed = changes.get("executed", previous["executed"])
    if state not in STATES or type(executed) is not bool:
        raise ValueError("Invalid execution observation")
    if previous["executed"] and not executed:
        raise ValueError("Execution cannot become unexecuted")
    if previous["state"] in TERMINAL and (state, executed) != (
        previous["state"],
        previous["executed"],
    ):
        raise ValueError("Terminal execution fact cannot change")
    if state in {"RUNNING", "SUCCEEDED"} and not executed:
        raise ValueError("Started state requires execution evidence")
    if state in {"QUEUED", "REJECTED", "SEALED"} and executed:
        raise ValueError("Unstarted state contradicts execution evidence")
