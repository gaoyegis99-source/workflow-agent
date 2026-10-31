"""Typed execution policies; runners compose them without callback hooks."""

from __future__ import annotations

from typing import Any, Protocol


class TracePolicy(Protocol):
    def context(
        self, row: dict[str, Any], event_type: str, epoch: int | None, source: str | None
    ) -> dict[str, Any] | None: ...


class PermitPolicy(Protocol):
    def request(self, row: dict[str, Any]) -> tuple[str, dict[str, Any]]: ...


class ActionTracePolicy:
    @staticmethod
    def workflow_execution(row: dict[str, Any]) -> str | None:
        if row.get("parent_execution_id"):
            return row["parent_execution_id"]
        if row.get("payload", {}).get("kind") == "WORKFLOW":
            return row["execution_id"]
        return None

    def context(self, row, _event_type, epoch, source):
        workflow = self.workflow_execution(row)
        if workflow is None:
            return None
        engine_epoch = (
            row["payload"].get("node_request", {}).get("epoch")
            if row.get("parent_execution_id")
            else row["runtime"].get("engine_epoch")
        )
        return {
            "workflow_execution_id": workflow,
            "node_action_id": row["action_id"] if row.get("parent_execution_id") else None,
            "engine_epoch": epoch if epoch is not None else engine_epoch,
            "worker_generation": row.get("generation"),
            "source": source or "EXECUTION_WORKER",
        }


class ActionPermitPolicy:
    def request(self, row):
        if row.get("parent_execution_id"):
            payload = row["payload"]
            return (
                f"/internal/workflows/{payload['parent_action_id']}/"
                f"{payload['parent_execution_id']}/nodes/{row['action_id']}/permit",
                payload["node_request"]
                | {"execution_id": row["execution_id"], "attempt_id": row["attempt_id"]},
            )
        return (
            "/internal/actions/" + row["action_id"] + "/permit",
            {"execution_id": row["execution_id"], "attempt_id": row["attempt_id"]},
        )


class WorkflowTracePolicy:
    def context(self, row, event_type, epoch, source):
        return {
            "workflow_execution_id": row["execution_id"],
            "engine_epoch": epoch if epoch is not None else row["runtime"].get("engine_epoch"),
            "worker_generation": row.get("generation"),
            "source": source
            or (
                "ENGINE"
                if event_type.startswith("ENGINE_") or event_type == "GATHER_FINISHED"
                else "DATA_CLIENT"
            ),
        }


class WorkflowPermitPolicy:
    def request(self, row):
        return (
            "/internal/actions/" + row["action_id"] + "/permit",
            {"execution_id": row["execution_id"], "attempt_id": row["attempt_id"]},
        )
