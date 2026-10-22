"""Fixed-image CWL validation adapter used only by the independent worker."""

import json
import re
import subprocess
import tempfile
from pathlib import Path


class CwlValidator:
    def __init__(self, image_id: str, timeout: float = 90):
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise ValueError("An immutable CWL image ID is required")
        self.image_id = image_id
        self.timeout = timeout

    def validate(self, compiled):
        with tempfile.TemporaryDirectory(prefix="v7-cwl-validate-") as directory:
            root = Path(directory)
            root.chmod(0o755)
            package = root / "package"
            package.mkdir()
            for name, document in compiled["files"].items():
                (package / name).write_text(json.dumps(document), encoding="utf-8")
            (root / "config.json").write_text(
                json.dumps({"compiled": {"entrypoint": compiled["entrypoint"]}}),
                encoding="utf-8",
            )
            try:
                result = subprocess.run(
                    [
                        "docker",
                        "run",
                        "--rm",
                        "--network",
                        "none",
                        "--read-only",
                        "--cpus",
                        "1",
                        "--memory",
                        "512m",
                        "--pids-limit",
                        "64",
                        "--cap-drop",
                        "ALL",
                        "--security-opt",
                        "no-new-privileges",
                        "--tmpfs",
                        "/tmp:rw,noexec,nosuid,size=64m",
                        "--user",
                        "65532:65532",
                        "--entrypoint",
                        "python",
                        "--mount",
                        f"type=bind,src={root},dst=/run/work,readonly",
                        "--mount",
                        f"type=bind,src={root / 'config.json'},dst=/run/config.json,readonly",
                        self.image_id,
                        "/opt/v7-cwl/engine.py",
                        "validate",
                    ],
                    capture_output=True,
                    timeout=self.timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as error:
                raise TimeoutError("Trusted CWL validation timed out") from error
            if result.returncode:
                # Validation output can contain submitted labels/docs. Do not
                # expose or log it; the stable public failure is enough.
                from platform_core.domain.control import require

                require(False, "INVALID_BINDING", "Compiled CWL package failed validation", 422)
