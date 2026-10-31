"""Artifact publication algorithms for actions and workflows."""

import hashlib

from platform_contracts.service import ensure

from .finalization import ExecutionLifecycle, WorkflowLifecycle
from .protocols import ActionComponent, ActionServices, WorkflowComponent, WorkflowServices


class ArtifactPublisher(ActionComponent):
    def __init__(self, services: ActionServices, lifecycle: ExecutionLifecycle):
        super().__init__(services)
        self.lifecycle = lifecycle

    def report_node(self, row, *, stopped=False):
        payload = row["payload"]
        revision = row["runtime"].get("node_revision", row["revision"])
        self.platform.json(
            "POST",
            f"/internal/workflows/{payload['parent_action_id']}/"
            f"{payload['parent_execution_id']}/nodes/{row['action_id']}/observations",
            json={
                "revision": revision,
                "state": row["runtime"].get("pending_state", row["state"]),
                "executed": row["executed"],
                "stopped": stopped,
                "execution_id": row["execution_id"],
                "attempt_id": row["attempt_id"],
            },
        )
        self.execution.trace(
            row,
            "NODE_OBSERVATION_REPORTED",
            [revision, stopped],
            attributes={
                "revision": revision,
                "state": row["runtime"].get("pending_state", row["state"]),
            },
        )

    def run(self, row, root):
        prefix = f"/internal/projects/{row['project_id']}/publications/{row['execution_id']}"
        manifest = row["runtime"]["manifest"]
        previous = self.data.json(
            "POST",
            prefix + "/prepare",
            json={"generation": row["generation"], "manifest": manifest},
        )
        if previous["state"] == "COMMITTED":
            receipt = previous["receipt"]
        else:
            if hasattr(self.platform, "json"):
                publication_permit = self.platform.json(
                    "POST", f"/internal/actions/{row['action_id']}/publication-permit"
                )
                if publication_permit.get("decision") != "ALLOW":
                    return self.lifecycle.abort(row, "REJECTED", "OPERATOR_REVOKED")
            for item in manifest["files"]:
                rep, filename = item["representation_id"], item.get("filename")
                if filename is None:
                    filename = {
                        "result": "result.json",
                        "table": "result.parquet",
                        "method": "method.json",
                    }[rep]
                self.data.request(
                    "PUT",
                    prefix + "/files/" + rep,
                    params={"generation": row["generation"]},
                    content=(root / "output" / filename).read_bytes(),
                )
            receipt = self.data.json(
                "POST", prefix + "/commit", json={"generation": row["generation"]}
            )
        return self.execution.transition(
            row,
            "FINAL",
            state="SUCCEEDED",
            publication="COMMITTED",
            publication_receipt=receipt,
        )


class WorkflowArtifactPublisher(WorkflowComponent):
    def __init__(self, services: WorkflowServices, lifecycle: WorkflowLifecycle):
        super().__init__(services)
        self.lifecycle = lifecycle

    def run(self, row, root):
        prefix = f"/internal/projects/{row['project_id']}/publications/{row['execution_id']}"
        manifest = row["runtime"]["manifest"]
        children = {child["action_id"]: child for child in self.repo.children(row["execution_id"])}
        ensure(
            children and all(child["state"] == "SUCCEEDED" for child in children.values()),
            "INVALID_BINDING",
            "Workflow children have not converged successfully",
        )
        for member in manifest["members"]:
            child = children.get(member["member_id"])
            ensure(
                child is not None
                and child["execution_id"] == member["child_execution_id"]
                and child["runtime"].get("publication") == "PRIVATE"
                and child["runtime"].get("manifest") == member["manifest"],
                "INVALID_BINDING",
                "Workflow publication member changed",
            )
        for item in manifest["files"]:
            child = children.get(item["member_id"])
            ensure(child is not None, "INVALID_BINDING", "Workflow publication member is missing")
            source = next(
                (
                    value
                    for value in child["runtime"].get("files", [])
                    if value["representation_id"] == item["original_representation_id"]
                ),
                None,
            )
            ensure(
                source is not None
                and all(
                    source[key] == item[key] for key in ("format", "digest", "size", "filename")
                ),
                "INVALID_BINDING",
                "Workflow publication file changed",
            )
            source_path = (
                root.parent / item["child_execution_id"] / "output" / item["filename"]
            ).resolve()
            child_output = (root.parent / item["child_execution_id"] / "output").resolve()
            ensure(
                source_path.is_relative_to(child_output)
                and source_path.is_file()
                and not source_path.is_symlink(),
                "INVALID_BINDING",
                "Private node output path changed",
            )
        # Re-read the authoritative cancellation fact after all potentially slow
        # child/file verification and before Data can prepare a publication.
        if self.repo.get(action=row["action_id"])["cancel_requested"]:
            return self.lifecycle.abort(row, "CANCELLED", "CANCELLED")
        previous = self.data.json(
            "POST",
            prefix + "/prepare",
            json={"generation": row["generation"], "manifest": manifest},
        )
        self.execution.trace(
            row,
            "PUBLICATION_PREPARED",
            [row["generation"], previous["state"]],
            attributes={"state": previous["state"]},
        )
        if previous["state"] == "COMMITTED":
            receipt = previous["receipt"]
        else:
            if hasattr(self.platform, "json"):
                publication_permit = self.platform.json(
                    "POST", f"/internal/actions/{row['action_id']}/publication-permit"
                )
                if publication_permit.get("decision") != "ALLOW":
                    return self.lifecycle.abort(row, "REJECTED", "OPERATOR_REVOKED")
            for item in manifest["files"]:
                source_path = (
                    root.parent / item["child_execution_id"] / "output" / item["filename"]
                ).resolve()
                content = source_path.read_bytes()
                ensure(
                    "sha256:" + hashlib.sha256(content).hexdigest() == item["digest"],
                    "INVALID_BINDING",
                    "Private node output changed",
                )
                self.data.request(
                    "PUT",
                    prefix + "/files/" + item["representation_id"],
                    params={"generation": row["generation"]},
                    content=content,
                )
            receipt = self.data.json(
                "POST", prefix + "/commit", json={"generation": row["generation"]}
            )
        self.execution.trace(
            row,
            "PUBLICATION_COMMITTED",
            [row["generation"], receipt["state"]],
            attributes={"state": receipt["state"]},
        )
        latest = self.repo.get(action=row["action_id"])
        details = {
            "publication": "COMMITTED",
            "publication_receipt": receipt,
        }
        if latest["cancel_requested"]:
            details["cancellation"] = {"state": "TOO_LATE_COMMITTED"}
        return self.execution.transition(row, "FINAL", state="SUCCEEDED", **details)
