"""Fixed Docker adapter for trusted spatial inspection."""

import json
import os
import re
import subprocess
from pathlib import Path


class SpatialInspector:
    def __init__(self, image_id: str, work_root: str):
        if re.fullmatch(r"sha256:[0-9a-f]{64}", image_id or "") is None:
            raise ValueError("Spatial inspector requires an immutable image ID")
        self.image_id = image_id
        self.work_root = Path(work_root).resolve()
        self.work_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def command(*args, timeout=10):
        result = subprocess.run(
            ["docker", *args], capture_output=True, check=False, timeout=timeout
        )
        if result.returncode:
            raise OSError("Spatial container runtime operation failed")
        return result.stdout.decode().strip()

    @staticmethod
    def name(upload_id):
        return "v7-spatial-import-" + upload_id

    def inspect(self, upload_id):
        result = subprocess.run(
            ["docker", "inspect", self.name(upload_id)],
            capture_output=True,
            check=False,
            timeout=5,
        )
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
        if os.environ.get("V7_TEST_RUN_ID"):
            expected["v7.test-run"] = os.environ["V7_TEST_RUN_ID"]
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
        return root, output

    def create(self, claim, source):
        _, output = self.paths(claim)
        source = Path(source).resolve()
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
            f"type=bind,src={source},dst={container_source},readonly",
            "--mount",
            f"type=bind,src={output},dst=/output",
        ]
        for key, value in (
            ("upload", claim["upload_id"]),
            ("digest", claim["payload"]["digest"]),
            ("format", claim["payload"]["format"]),
        ):
            args += ["--label", f"v7.{key}={value}"]
        if os.environ.get("V7_TEST_RUN_ID"):
            args += ["--label", f"v7.test-run={os.environ['V7_TEST_RUN_ID']}"]
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
