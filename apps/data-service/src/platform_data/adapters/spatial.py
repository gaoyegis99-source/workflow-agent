"""Fixed Docker adapter for trusted spatial inspection."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from platform_contracts.runtime_context import ContainerIdentity

DOCKER_COMMAND_TIMEOUT_SECONDS = 30


class SpatialInspector:
    def __init__(
        self,
        image_id: str,
        work_root: str,
        *,
        container_identity: ContainerIdentity | None = None,
    ):
        if re.fullmatch(r"sha256:[0-9a-f]{64}", image_id or "") is None:
            raise ValueError("Spatial inspector requires an immutable image ID")
        self.image_id = image_id
        self.work_root = Path(work_root).resolve()
        self.container_identity = container_identity or ContainerIdentity()
        self.work_root.mkdir(parents=True, exist_ok=True)
        # The rootless daemon resolves bind sources as the host user, while
        # this worker writes them as its remapped uid.  Permit traversal only;
        # individual staged inputs remain read-only.
        try:
            self.work_root.chmod(0o711)
        except PermissionError:
            # Direct Compose prepares this bind root behind a 0700 instance
            # directory.  Rootless namespace ownership prevents this non-root
            # process from changing the mount point itself, so accept only an
            # already writable and traversable root.
            if not os.access(self.work_root, os.W_OK | os.X_OK):
                raise

    @staticmethod
    def command(*args, timeout=DOCKER_COMMAND_TIMEOUT_SECONDS):
        try:
            result = subprocess.run(
                ["docker", *args], capture_output=True, check=False, timeout=timeout
            )
        except subprocess.TimeoutExpired as error:
            operation = args[0] if args else "unknown"
            raise OSError(f"Spatial container runtime {operation} timed out") from error
        if result.returncode:
            raise OSError("Spatial container runtime operation failed")
        return result.stdout.decode().strip()

    @staticmethod
    def name(upload_id):
        return "v7-spatial-import-" + upload_id

    def inspect(self, upload_id):
        try:
            result = subprocess.run(
                ["docker", "inspect", self.name(upload_id)],
                capture_output=True,
                check=False,
                timeout=DOCKER_COMMAND_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as error:
            raise OSError("Spatial container runtime inspect timed out") from error
        if result.returncode:
            self.command("info", "--format", "{{.ServerVersion}}")
            if b"No such" in result.stderr:
                return None
            raise OSError("Spatial container inspect failed")
        return json.loads(result.stdout)[0]

    def check(self, claim, info):
        labels = info["Config"]["Labels"] or {}
        expected = {
            "v7.upload": claim["upload_id"],
            "v7.digest": claim["payload"]["digest"],
            "v7.format": claim["payload"]["format"],
        }
        expected |= self.container_identity.labels()
        if any(labels.get(key) != value for key, value in expected.items()):
            raise ValueError("Spatial inspection container identity differs")
        if info["Image"] != self.image_id:
            raise ValueError("Spatial inspection image differs")

    def paths(self, claim):
        root = (self.work_root / claim["upload_id"]).resolve()
        if not root.is_relative_to(self.work_root):
            raise ValueError("Spatial inspection work path escaped")
        output = root / "output"
        output.mkdir(parents=True, exist_ok=True)
        # A rootless daemon remaps the requested container UID.  Grant that
        # remapped UID create access to this per-upload output directory; the
        # parent remains traversal-only and the staged input stays read-only.
        output.chmod(0o733)
        return root, output

    def create(self, claim, source):
        root, output = self.paths(claim)
        source = Path(source).resolve()
        staged_source = root / (
            "source.gpkg" if claim["payload"]["format"] == "geopackage" else "source.tif"
        )
        shutil.copyfile(source, staged_source)
        staged_source.chmod(0o444)
        container_source = (
            "/input/source.gpkg"
            if claim["payload"]["format"] == "geopackage"
            else "/input/source.tif"
        )
        args = [
            "create",
            "--name",
            self.name(claim["upload_id"]),
            "--label",
            "v7.phase=P4.2.1-import",
            "--user",
            f"{os.getuid() or 65532}:{os.getgid() or 65532}",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--cpus",
            "1",
            "--memory",
            "1g",
            "--memory-swap",
            "1g",
            "--pids-limit",
            "128",
            "--ulimit",
            "fsize=104857600:104857600",
            "--ulimit",
            "core=0:0",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=128m",
            "--mount",
            f"type=bind,src={staged_source},dst={container_source},readonly",
            "--mount",
            f"type=bind,src={output},dst=/output",
        ]
        for key, value in (
            ("upload", claim["upload_id"]),
            ("digest", claim["payload"]["digest"]),
            ("format", claim["payload"]["format"]),
        ):
            args += ["--label", f"v7.{key}={value}"]
        for key, value in self.container_identity.labels().items():
            args += ["--label", f"{key}={value}"]
        args += [
            self.image_id,
            "platform-spatial",
            "inspect",
            "--format",
            claim["payload"]["format"],
            "--input",
            container_source,
            "--output",
            "/output/inspection.json",
        ]
        self.command(*args)
        return self.inspect(claim["upload_id"])

    def start(self, container_id):
        self.command("start", container_id)

    def remove(self, container_id):
        try:
            self.command("rm", "-f", container_id)
        except OSError:
            pass

    def stop(self, container_id):
        try:
            self.command("kill", container_id)
        except OSError:
            pass
