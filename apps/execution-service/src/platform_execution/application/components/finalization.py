"""Terminal state and cancellation algorithms."""

from .protocols import ActionComponent, WorkflowComponent


class ExecutionLifecycle(ActionComponent):
    def abort(self, row, state, error=None, **details):
        if row.get("parent_execution_id"):
            return self.execution.transition(
                row,
                "REPORTING",
                pending_state=state,
                node_revision=row["revision"] + 1,
                error=error,
                publication="PRIVATE",
                **details,
            )
        if row["payload"].get("kind") == "CHECK":
            return self.execution.transition(
                row,
                "FINAL",
                state=state,
                publication="NOT_REQUIRED",
                error=error,
                **details,
            )
        prefix = f"/internal/projects/{row['project_id']}/publications/{row['execution_id']}"
        receipt = self.data.json("POST", prefix + "/abort", json={"generation": row["generation"]})
        details = details | {
            "publication": receipt["state"],
            "publication_receipt": receipt,
            "error": error,
        }
        if state == "CANCELLED":
            if receipt["state"] == "COMMITTED":
                state = "SUCCEEDED"
                details["cancellation"] = {"state": "TOO_LATE_COMMITTED"}
            else:
                details["cancellation"] = {"state": "ABORTED_BEFORE_COMMIT"}
        return self.execution.transition(row, "FINAL", state=state, **details)

    def cancel(self, row, info):
        if info is None and row["runtime"]["phase"] == "START_INTENT" and not row["executed"]:
            raise OSError("Cannot prove whether the missing container executed after start intent")
        if info:
            self.docker.check(row, info)
            started = not info["State"]["StartedAt"].startswith("0001-")
            if started and not row["executed"]:
                if not self.execution.transition(
                    row, row["runtime"]["phase"], executed=True, state="RUNNING"
                ):
                    return
            if info["State"]["Running"] or not started:
                self.docker.stop(info["Id"], never_started=not started)
                return
        self.abort(row, "CANCELLED")


class WorkflowLifecycle(WorkflowComponent):
    def abort(self, row, state, error):
        prefix = f"/internal/projects/{row['project_id']}/publications/{row['execution_id']}"
        receipt = self.data.json("POST", prefix + "/abort", json={"generation": row["generation"]})
        self.execution.trace(
            row,
            "PUBLICATION_ABORTED",
            [row["generation"], receipt["state"]],
            attributes={"state": receipt["state"]},
        )
        details = {
            "publication": receipt["state"],
            "publication_receipt": receipt,
            "error": error,
        }
        if error == "CANCELLED":
            if receipt["state"] == "COMMITTED":
                state = "SUCCEEDED"
                details["cancellation"] = {"state": "TOO_LATE_COMMITTED"}
            else:
                details["cancellation"] = {"state": "ABORTED_BEFORE_COMMIT"}
        return self.execution.transition(row, "FINAL", state=state, **details)
