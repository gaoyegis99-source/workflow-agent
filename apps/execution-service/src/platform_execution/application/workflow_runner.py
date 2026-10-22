"""Recoverable Toil supervisor for frozen workflow packages."""

import hashlib
import json
import os
import secrets
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from platform_contracts.engine_runtime import profile
from platform_contracts.engine_runtime import validate as validate_runtime_profile
from platform_contracts.service import ensure

from platform_execution.engine_package import validate as validate_engine_package
from platform_execution.trace import record as trace


class WorkflowRunner:
    def __init__(self, repository, docker, data, platform, execution_url):
        self.repo, self.docker = repository, docker
        self.data, self.platform = data, platform
        ensure(
            execution_url.startswith(("http://", "https://")),
            "INVALID_BINDING",
            "Workflow callback URL is required",
        )
        self.execution_url = execution_url.rstrip("/")

    @staticmethod
    def save(path, content, mode=0o444):
        if path.exists():
            ensure(path.read_bytes() == content, "INVALID_BINDING", "Frozen workflow file changed")
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        with temporary.open("wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(mode)

    def runtime(self, row, **changes):
        value = row["runtime"] | changes
        value.pop("uncertain", None)
        value.pop("failures", None)
        return self.repo.change(row, runtime=value)

    @staticmethod
    def engine_profile(row):
        value = row["runtime"].get("_engine_runtime_profile")
        return validate_runtime_profile(value) if value is not None else profile()

    def trace(
        self, row, event_type, discriminator, *, duration_ms=None, attributes=None, epoch=None
    ):
        return trace(
            self.repo,
            workflow_execution_id=row["execution_id"],
            engine_epoch=epoch if epoch is not None else row["runtime"].get("engine_epoch"),
            worker_generation=row.get("generation"),
            source=(
                "ENGINE"
                if event_type.startswith("ENGINE_") or event_type == "GATHER_FINISHED"
                else "DATA_CLIENT"
            ),
            event_type=event_type,
            discriminator=discriminator,
            source_monotonic_ns=time.monotonic_ns(),
            duration_ms=duration_ms,
            attributes=attributes,
        )

    def permit(self, row):
        started = time.perf_counter_ns()
        trace(
            self.repo,
            workflow_execution_id=row["execution_id"],
            engine_epoch=row["runtime"].get("engine_epoch"),
            worker_generation=row.get("generation"),
            source="PLATFORM_CLIENT",
            event_type="PERMIT_REQUESTED",
            discriminator=[row["generation"], row["runtime"]["phase"]],
            source_monotonic_ns=time.monotonic_ns(),
            attributes={"phase": row["runtime"]["phase"]},
        )
        result = self.platform.json(
            "POST",
            "/internal/actions/" + row["action_id"] + "/permit",
            json={"execution_id": row["execution_id"], "attempt_id": row["attempt_id"]},
        )
        event = {"ALLOW": "PERMIT_ALLOWED", "WAIT": "PERMIT_WAIT"}.get(
            result["decision"], "PERMIT_DENIED"
        )
        trace(
            self.repo,
            workflow_execution_id=row["execution_id"],
            engine_epoch=row["runtime"].get("engine_epoch"),
            worker_generation=row.get("generation"),
            source="PLATFORM_CLIENT",
            event_type=event,
            discriminator=[row["generation"], row["runtime"]["phase"], result["decision"]],
            source_monotonic_ns=time.monotonic_ns(),
            duration_ms=(time.perf_counter_ns() - started) / 1_000_000,
            attributes={"decision": result["decision"]},
        )
        return result

    def stage(self, row, root):
        engine_package = validate_engine_package(row["payload"])
        engine = engine_package or row["payload"]["compiled"]
        package = root / "package"
        package.mkdir(exist_ok=True)
        for name, document in engine["files"].items():
            self.save(package / name, json.dumps(document, sort_keys=True).encode())
        if engine_package is not None:
            self.save(
                root / "engine-package.json",
                json.dumps(engine_package, sort_keys=True, separators=(",", ":")).encode(),
            )

        counter = 0

        def materialize(value):
            nonlocal counter
            if isinstance(value, dict) and value.get("class") == "File":
                binding = value["binding"]
                selection, representation = binding["selection"], binding["representation"]
                ref = selection["asset"]
                path = (
                    f"/internal/projects/{row['project_id']}/assets/{ref['asset_id']}/versions/"
                    f"{ref['version_id']}/representations/{selection['representation_id']}/content"
                )
                content = self.data.request("GET", path).content
                ensure(
                    "sha256:" + hashlib.sha256(content).hexdigest() == representation["digest"],
                    "INVALID_BINDING",
                    "Workflow input digest mismatch",
                )
                counter += 1
                filename = f"input-{counter}-{representation['digest'][7:23]}"
                self.save(root / "input" / filename, content)
                return {"class": "File", "path": "/run/work/input/" + filename}
            if isinstance(value, dict):
                return {key: materialize(item) for key, item in value.items()}
            if isinstance(value, list):
                return [materialize(item) for item in value]
            return value

        inputs = materialize(row["payload"]["inputs"])
        if engine_package is None or engine_package["strategy"] == "legacy_v1":
            inputs["v7_context"] = "[]"
        self.save(root / "inputs.json", json.dumps(inputs, sort_keys=True).encode())
        token_path = root / "bridge.token"
        if not token_path.exists():
            self.save(token_path, secrets.token_urlsafe(48).encode(), 0o400)
        token = token_path.read_text()
        facts = (
            {
                "engine_strategy": engine_package["strategy"],
                "engine_package_digest": engine_package["package_digest"],
            }
            if engine_package is not None
            else {"engine_strategy": "legacy_v1", "engine_package_digest": None}
        )
        return self.runtime(
            row,
            phase="PREPARED",
            publication="UNKNOWN",
            image_id=self.docker.image,
            bridge_token_digest=hashlib.sha256(token.encode()).hexdigest(),
            engine_epoch=0,
            **facts,
        )

    def configure(self, row, root, epoch, restart):
        engine_package = validate_engine_package(row["payload"])
        config = {
            "compiled": row["payload"]["compiled"],
            "execution_id": row["execution_id"],
            "execution_url": self.execution_url,
            "token": (root / "bridge.token").read_text(),
            "epoch": epoch,
            "restart": restart,
            "engine_runtime_profile": self.engine_profile(row),
            "release_digest": os.environ.get("V7_RELEASE_DIGEST"),
        }
        if engine_package is not None:
            config["engine_package"] = engine_package
        path = root / f"config-{epoch}.json"
        self.save(path, json.dumps(config, sort_keys=True).encode(), 0o400)
        return path

    @staticmethod
    def restartable_jobstore(root):
        """Recognize the pinned Toil file JobStore only after its root is durable."""
        return (root / "jobstore" / "shared" / "rootJobStoreID").is_file()

    @staticmethod
    def maintenance_restart(row, root, epoch):
        """Authorize exactly the epoch recorded by a verified P8 park manifest."""

        path = os.environ.get("V7_PARK_MANIFEST")
        if not path or not Path(path).is_file():
            return False
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
            supplied = value.pop("manifest_digest")
            payload = json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
            if supplied != "sha256:" + hashlib.sha256(payload).hexdigest():
                return False
            if value.get("schema_version") != 1 or value.get("state") != "PARKED":
                return False
            record = next(
                item
                for item in value.get("jobstores", [])
                if item.get("action_id") == row["action_id"]
                and item.get("execution_id") == row["execution_id"]
                and item.get("engine_epoch") == epoch
            )
            if Path(record["jobstore"]).resolve() != (Path(root) / "jobstore").resolve():
                return False
            for item in record.get("key_files", []):
                current = Path(record["jobstore"]) / item["path"]
                if (
                    current.stat().st_size != item["size"]
                    or "sha256:" + hashlib.sha256(current.read_bytes()).hexdigest()
                    != item["digest"]
                ):
                    return False
            return bool(record.get("restartable"))
        except (KeyError, OSError, StopIteration, TypeError, ValueError, json.JSONDecodeError):
            return False

    @staticmethod
    def exit_fact(root, epoch):
        """Read a complete exit fact for this epoch, including the legacy shape."""
        path = root / f"exit-{epoch}.json"
        try:
            value = json.loads(path.read_text())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if type(value) is not dict:
            return None
        if type(value.get("epoch")) is not int or value["epoch"] != epoch:
            return None
        if type(value.get("returncode")) is not int:
            return None
        if "schema_version" not in value:
            return value if set(value) == {"epoch", "returncode"} else None
        if (
            value.get("schema_version") != 1
            or set(value)
            != {
                "schema_version",
                "epoch",
                "returncode",
                "termination_signal",
                "forced",
            }
            or value.get("termination_signal") not in {None, "SIGTERM", "SIGINT"}
            or type(value.get("forced")) is not bool
        ):
            return None
        return value

    def abort(self, row, state, error):
        prefix = f"/internal/projects/{row['project_id']}/publications/{row['execution_id']}"
        receipt = self.data.json("POST", prefix + "/abort", json={"generation": row["generation"]})
        self.trace(
            row,
            "PUBLICATION_ABORTED",
            [row["generation"], receipt["state"]],
            attributes={"state": receipt["state"]},
        )
        runtime = row["runtime"] | {
            "phase": "FINAL",
            "publication": receipt["state"],
            "publication_receipt": receipt,
            "error": error,
        }
        if error == "CANCELLED":
            if receipt["state"] == "COMMITTED":
                state = "SUCCEEDED"
                runtime["cancellation"] = {"state": "TOO_LATE_COMMITTED"}
            else:
                runtime["cancellation"] = {"state": "ABORTED_BEFORE_COMMIT"}
        return self.repo.change(
            row,
            state=state,
            runtime=runtime,
        )

    def _result_path(self, root, value):
        location = value.get("location") or value.get("path")
        ensure(isinstance(location, str), "INVALID_BINDING", "Workflow File has no location")
        if location.startswith("file://"):
            location = unquote(urlparse(location).path)
        prefix = "/run/work/"
        ensure(location.startswith(prefix), "INVALID_BINDING", "Workflow output escaped work area")
        path = (root / location[len(prefix) :]).resolve()
        ensure(
            path.is_relative_to(root) and path.is_file() and not path.is_symlink(),
            "INVALID_BINDING",
            "Workflow output is unavailable",
        )
        return path

    def collect(self, row, root):
        if "payload" in row:
            validate_engine_package(row["payload"])
        epoch = row["runtime"]["engine_epoch"]
        result = json.loads((root / f"result-{epoch}.json").read_text())
        children = self.repo.children(row["execution_id"])
        matches = {}
        for child in children:
            for item in child["runtime"].get("files", []):
                name = child["action_id"] + "--" + item["representation_id"]
                matches.setdefault(name, []).append((child, item))
        members = {}

        def freeze(value, path):
            if isinstance(value, dict) and value.get("class") == "File":
                source = self._result_path(root, value)
                content = source.read_bytes()
                fingerprint = "sha256:" + hashlib.sha256(content).hexdigest()
                candidates = matches.get(source.name, [])
                ensure(
                    len(candidates) == 1 and candidates[0][1]["digest"] == fingerprint,
                    "INVALID_BINDING",
                    "Output provenance is ambiguous or changed",
                )
                child, item = candidates[0]
                member = members.setdefault(
                    child["action_id"],
                    {
                        "member_id": child["action_id"],
                        "child_execution_id": child["execution_id"],
                        "manifest": child["runtime"]["manifest"],
                    },
                )
                return {
                    "member_id": member["member_id"],
                    "representation_id": item["representation_id"],
                }
            if isinstance(value, dict):
                return {key: freeze(item, path + [key]) for key, item in value.items()}
            if isinstance(value, list):
                return [freeze(item, path + [index]) for index, item in enumerate(value)]
            return value

        outputs = freeze(result, [])
        files = []
        total = 0
        for member in members.values():
            child = next(item for item in children if item["action_id"] == member["member_id"])
            for item in child["runtime"]["files"]:
                total += item["size"]
                files.append(
                    {
                        "representation_id": member["member_id"] + "--" + item["representation_id"],
                        "member_id": member["member_id"],
                        "original_representation_id": item["representation_id"],
                        "format": item["format"],
                        "digest": item["digest"],
                        "size": item["size"],
                        **(
                            {"schema_id": item["schema_id"]}
                            if item.get("schema_id") is not None
                            else {}
                        ),
                        "child_execution_id": child["execution_id"],
                        "filename": item["filename"],
                    }
                )
        ensure(total <= 100 * 1024 * 1024, "INVALID_BINDING", "Workflow output limit exceeded")
        manifest = {
            "kind": "WORKFLOW_RESULT",
            "execution_id": row["execution_id"],
            "attempt_id": row["attempt_id"],
            "project_id": row["project_id"],
            "task_ref": {
                "task_id": row["task_id"],
                "spec_revision": row["payload"]["expected_spec_revision"],
            },
            "target": row["payload"]["target"],
            "outputs": outputs,
            "members": list(members.values()),
            "files": files,
        }
        self.trace(
            row,
            "GATHER_FINISHED",
            [row["runtime"]["engine_epoch"], len(members)],
            attributes={"member_count": len(members)},
        )
        return self.runtime(row, phase="PUBLISHING", manifest=manifest)

    def publish(self, row, root):
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
            return self.abort(row, "CANCELLED", "CANCELLED")
        previous = self.data.json(
            "POST",
            prefix + "/prepare",
            json={"generation": row["generation"], "manifest": manifest},
        )
        self.trace(
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
                    return self.abort(row, "REJECTED", "OPERATOR_REVOKED")
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
        self.trace(
            row,
            "PUBLICATION_COMMITTED",
            [row["generation"], receipt["state"]],
            attributes={"state": receipt["state"]},
        )
        latest = self.repo.get(action=row["action_id"])
        runtime = row["runtime"] | {
            "phase": "FINAL",
            "publication": "COMMITTED",
            "publication_receipt": receipt,
        }
        if latest["cancel_requested"]:
            runtime["cancellation"] = {"state": "TOO_LATE_COMMITTED"}
        return self.repo.change(
            row,
            state="SUCCEEDED",
            runtime=runtime,
        )

    def step(self, row, root):
        if not self.repo.change(row):
            return
        if "payload" in row:
            validate_engine_package(row["payload"])
        if "_engine_runtime_profile" not in row["runtime"]:
            if not self.runtime(
                row,
                _engine_runtime_profile=profile(),
                _engine_profile_legacy_unlabeled=True,
            ):
                return None
        phase = row["runtime"]["phase"]
        epoch = row["runtime"].get("engine_epoch", 0)
        info = self.docker.inspect(row["execution_id"], epoch) if epoch else None
        if row["cancel_requested"]:
            if row["runtime"].get("cancellation", {}).get("state") != "PENDING_REMOTE":
                pending = row["runtime"] | {"cancellation": {"state": "PENDING_REMOTE"}}
                if not self.repo.change(row, runtime=pending):
                    return
                row["runtime"] = pending
            if info and info["State"]["Running"]:
                self.docker.check(row, info, epoch)
                info = self.docker.stop(info["Id"])
                if info is not None and info["State"]["Running"]:
                    return
            children = self.repo.cancel_children(row["execution_id"])
            if any(
                child["state"] not in {"SUCCEEDED", "FAILED", "CANCELLED", "REJECTED", "SEALED"}
                for child in children
            ):
                return
            # A queued cancellation and a permit denial are the same verified fact:
            # this workflow never started.  Keep the terminal observation stable
            # regardless of which notification wins the Platform/Execution race.
            state = "CANCELLED" if row["executed"] else "REJECTED"
            return self.abort(row, state, "CANCELLED")
        if phase == "ACCEPTED":
            return self.stage(row, root)
        if phase in {"PREPARED", "RESTART_READY"}:
            permit = self.permit(row)
            if permit["decision"] == "WAIT":
                return
            if permit["decision"] != "ALLOW":
                return self.abort(row, "REJECTED", "PERMIT_DENIED")
            restart = phase == "RESTART_READY"
            epoch = epoch + 1
            ensure(
                not restart or self.restartable_jobstore(root),
                "INVALID_BINDING",
                "Recoverable JobStore root is missing",
            )
            config = self.configure(row, root, epoch, restart)
            info = self.docker.create(row, root, epoch, config)
            self.docker.check(row, info, epoch)
            self.trace(
                row,
                "ENGINE_CREATED",
                [epoch, info["Id"]],
                epoch=epoch,
                attributes={"container_id": info["Id"]},
            )
            return self.runtime(
                row, phase="ENGINE_CREATED", engine_epoch=epoch, container_id=info["Id"]
            )
        if phase == "ENGINE_CREATED":
            ensure(info is not None, "INVALID_BINDING", "Workflow engine disappeared before start")
            self.docker.check(row, info, epoch)
            self.docker.start(info["Id"])
            self.trace(
                row,
                "ENGINE_STARTED",
                [epoch, info["Id"]],
                epoch=epoch,
                attributes={"container_id": info["Id"]},
            )
            return self.repo.change(
                row,
                state="RUNNING",
                executed=True,
                runtime=row["runtime"] | {"phase": "ENGINE_RUNNING"},
            )
        if phase == "ENGINE_RUNNING":
            ensure(info is not None, "INVALID_BINDING", "Workflow engine identity unavailable")
            self.docker.check(row, info, epoch)
            if info["State"]["Running"]:
                started = datetime.fromisoformat(info["State"]["StartedAt"].replace("Z", "+00:00"))
                ensure(
                    (datetime.now(UTC) - started).total_seconds() <= 3600,
                    "INVALID_BINDING",
                    "Workflow time limit exceeded",
                )
                return
            self.trace(
                row,
                "ENGINE_EXITED",
                [epoch, info["Id"]],
                epoch=epoch,
                attributes={
                    "container_id": info["Id"],
                    "exit_code": info["State"].get("ExitCode"),
                },
            )
            exit_fact = self.exit_fact(root, epoch)
            if exit_fact is None:
                if (
                    epoch == 1 or self.maintenance_restart(row, root, epoch)
                ) and self.restartable_jobstore(root):
                    return self.runtime(row, phase="RESTART_READY", first_exit="INTERRUPTED")
                return self.abort(row, "FAILED", "ENGINE_EXIT_UNKNOWN")
            code = exit_fact["returncode"]
            if code == 0:
                return self.collect(row, root)
            children = self.repo.children(row["execution_id"])
            if any(
                child["state"] in {"FAILED", "REJECTED", "CANCELLED", "SEALED"}
                for child in children
            ):
                return self.abort(row, "FAILED", "NODE_FAILED")
            if (
                epoch == 1 or self.maintenance_restart(row, root, epoch)
            ) and self.restartable_jobstore(root):
                return self.runtime(row, phase="RESTART_READY", first_exit=code)
            # A restarted Toil leader can retain the first leader's non-zero status even
            # after every platform node was recovered by its stable identity.  Accept that
            # recovery only when every observed child succeeded and the frozen output can
            # be independently collected and provenance-checked.  A missing/partial result
            # or any failed child still follows the failure path below.
            if children and all(child["state"] == "SUCCEEDED" for child in children):
                try:
                    return self.collect(row, root)
                except (ValueError, KeyError, FileNotFoundError):
                    pass
            return self.abort(row, "FAILED", "WORKFLOW_FAILED")
        if phase == "PUBLISHING":
            return self.publish(row, root)
        raise ValueError("Unknown workflow phase")
