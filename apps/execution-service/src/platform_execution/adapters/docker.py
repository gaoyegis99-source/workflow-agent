"""Trusted one-attempt Docker runtime. Never accept client commands or image names."""

import json
import os
import re
import subprocess
from pathlib import Path

from platform_contracts.engine_runtime import validate as validate_runtime_profile

from platform_execution.implementations import implementation


class Docker:
    def __init__(
        self,
        operator_image,
        checker_image,
        qgis_image=None,
        spatial_native_image=None,
        gdal_checker_image=None,
        tabular_image=None,
    ):
        self.images = {
            "OPERATOR": operator_image,
            "CHECK": checker_image,
            "QGIS": qgis_image,
            "SPATIAL_NATIVE": spatial_native_image,
            "GDAL_CHECKER": gdal_checker_image,
            "TABULAR": tabular_image,
        }
        if not all(
            value is None or re.fullmatch(r"sha256:[0-9a-f]{64}", value or "")
            for value in self.images.values()
        ):
            raise ValueError("Runtime requires locally verified immutable image IDs")

    def profile(self, row):
        identity, spec = implementation(row["payload"])
        image = self.images.get(spec["image_key"])
        if image is None:
            raise OSError("Registered implementation image is unavailable")
        if spec["protocol"] == "CAPABILITY_CHECK":
            run = row["payload"].get("checker_run", {})
            if (
                run.get("implementation_id") != identity
                or run.get("image_digest") != image
                or run.get("capability_id") != identity
            ):
                raise ValueError("Checker runtime differs from its frozen capability")
        frozen = row["payload"].get("_operator_snapshot")
        # Production and explicitly catalogued development deployments must run
        # the exact frozen image. Legacy development tests deliberately swap a
        # source-catalogue image to inject deterministic bad scientific output.
        enforce_catalog_image = os.environ.get(
            "V7_DEPLOYMENT_MODE", "development"
        ) == "production" or bool(os.environ.get("V7_OPERATOR_CATALOG"))
        if frozen is not None and enforce_catalog_image:
            executable = frozen.get("implementation") or {}
            if executable.get("image_digest") != image:
                raise ValueError("Operator Catalog image differs from the installed runtime")
        return {
            "implementation_id": identity,
            "image_id": image,
            **spec,
            **(
                {
                    "operator_digest": frozen["operator_digest"],
                    "implementation_digest": frozen["implementation"]["implementation_digest"],
                    "ownership": frozen["ownership"],
                    "trust_level": frozen["trust_level"],
                    "catalog_digest": frozen["catalog_digest"],
                }
                if frozen is not None
                else {}
            ),
        }

    @staticmethod
    def name(row):
        profile = row.get("runtime", {}).get("implementation_profile", {})
        protocol = profile.get("protocol", "")
        prefix = (
            "v7-p42-"
            if protocol in {"SPATIAL_VECTOR", "SPATIAL_SUMMARY", "CARTOGRAPHIC_PNG"}
            else ("v7-p71a-" if protocol == "TABULAR_V1" else "v7-p3-")
        )
        return prefix + row["execution_id"]

    @staticmethod
    def command(*args, timeout=5):
        result = subprocess.run(
            ["docker", *args], capture_output=True, timeout=timeout, check=False
        )
        if result.returncode:
            raise OSError("Container runtime operation failed")
        return result.stdout.decode().strip()

    def inspect(self, name):
        result = subprocess.run(["docker", "inspect", name], capture_output=True, timeout=5)
        if result.returncode:
            # Differentiate a functioning daemon's confirmed absence from daemon failure.
            self.command("info", "--format", "{{.ServerVersion}}")
            if b"No such" in result.stderr:
                return None
            raise OSError("Container inspect failed")
        return json.loads(result.stdout)[0]

    def check(self, row, info):
        labels = info["Config"]["Labels"] or {}
        expected = {
            "v7.action": row["action_id"],
            "v7.digest": row["digest"],
            "v7.attempt": row["attempt_id"],
        }
        profile = row["runtime"].get("implementation_profile")
        if profile is not None:
            expected["v7.implementation"] = profile["implementation_id"]
        if os.environ.get("V7_TEST_RUN_ID"):
            expected["v7.test-run"] = os.environ["V7_TEST_RUN_ID"]
        p82 = {
            "v7.execution": row["execution_id"],
            "v7.deployment": os.environ.get("V7_DEPLOYMENT_ID"),
            "v7.release": os.environ.get("V7_RELEASE_DIGEST"),
        }
        # N-1 containers have none of the P8.2 labels.  A partial set is never
        # accepted; new containers must carry the complete release identity.
        if any(labels.get(key) is not None for key in p82):
            expected |= {key: value for key, value in p82.items() if value is not None}
        if any(labels.get(key) != value for key, value in expected.items()):
            raise ValueError("Container identity labels differ")
        if row["container_id"] and info["Id"] != row["container_id"]:
            raise ValueError("Container identity differs")
        if info["Image"] != row["runtime"]["image_id"]:
            raise ValueError("Container image differs")

    def create(self, row, root):
        root = Path(root).resolve()
        name = self.name(row)
        profile = row["runtime"].get("implementation_profile") or self.profile(row)
        args = [
            "create",
            "--name",
            name,
            "--label",
            "v7.phase="
            + (
                "P4.2.1"
                if profile["protocol"] in {"SPATIAL_VECTOR", "SPATIAL_SUMMARY", "CARTOGRAPHIC_PNG"}
                else ("P7.1a" if profile["protocol"] == "TABULAR_V1" else "P3")
            ),
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
            profile["cpus"],
            "--memory",
            profile["memory"],
            "--memory-swap",
            profile["memory"],
            "--pids-limit",
            profile["pids"],
            "--ulimit",
            "fsize=104857600:104857600",
            "--ulimit",
            "core=0:0",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=16m",
            "--log-driver",
            "none",
            "--mount",
            f"type=bind,src={root / 'input'},dst=/input,readonly",
            "--mount",
            f"type=bind,src={root / 'output'},dst=/output",
            "-e",
            "OPENBLAS_NUM_THREADS=1",
            "-e",
            "OMP_NUM_THREADS=1",
        ]
        for key, value in [
            ("action", row["action_id"]),
            ("execution", row["execution_id"]),
            ("digest", row["digest"]),
            ("attempt", row["attempt_id"]),
            ("implementation", profile["implementation_id"]),
        ]:
            args += ["--label", f"v7.{key}={value}"]
        for key, variable in (
            ("deployment", "V7_DEPLOYMENT_ID"),
            ("release", "V7_RELEASE_DIGEST"),
            ("test-run", "V7_TEST_RUN_ID"),
        ):
            if os.environ.get(variable):
                args += ["--label", f"v7.{key}={os.environ[variable]}"]
        args += [row["runtime"]["image_id"]]
        self.command(*args)
        return self.inspect(name)

    def start(self, container):
        self.command("start", container)

    def stop(self, container, *, never_started=False):
        if never_started:
            self.command("rm", container)
        else:
            self.command("kill", container)


class CwlDocker:
    """Trusted Toil engine container; it receives no database or Docker socket."""

    def __init__(self, image):
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image or ""):
            raise ValueError("Workflow runtime requires an immutable image ID")
        self.image = image

    command = staticmethod(Docker.command)

    @staticmethod
    def name(execution, epoch):
        return f"v7-p4-{execution}-e{epoch}"

    def inspect(self, execution, epoch):
        return Docker.inspect(self, self.name(execution, epoch))

    def check(self, row, info, epoch):
        labels = info["Config"]["Labels"] or {}
        expected = {
            "v7.action": row["action_id"],
            "v7.digest": row["digest"],
            "v7.attempt": row["attempt_id"],
            "v7.epoch": str(epoch),
        }
        p82 = {
            "v7.execution": row["execution_id"],
            "v7.deployment": os.environ.get("V7_DEPLOYMENT_ID"),
            "v7.release": os.environ.get("V7_RELEASE_DIGEST"),
        }
        if os.environ.get("V7_TEST_RUN_ID"):
            expected["v7.test-run"] = os.environ["V7_TEST_RUN_ID"]
        if any(labels.get(key) is not None for key in p82):
            expected |= {key: value for key, value in p82.items() if value is not None}
        engine = row.get("payload", {}).get("engine_package")
        if engine is not None:
            expected |= {
                "v7.engine.strategy": engine["strategy"],
                "v7.engine.digest": engine["package_digest"],
            }
        runtime_profile = row.get("runtime", {}).get("_engine_runtime_profile")
        legacy_unlabeled = row.get("runtime", {}).get("_engine_profile_legacy_unlabeled", False)
        if runtime_profile is not None and not legacy_unlabeled:
            runtime_profile = validate_runtime_profile(runtime_profile)
            expected |= {
                "v7.engine.profile": runtime_profile["profile_id"],
                "v7.engine.profile-digest": runtime_profile["profile_digest"],
            }
        if any(labels.get(key) != value for key, value in expected.items()):
            raise ValueError("Workflow engine labels differ")
        if info["Image"] != self.image:
            raise ValueError("Workflow engine image differs")

    def create(self, row, root, epoch, config):
        root = Path(root).resolve()
        config = Path(config).resolve()
        args = [
            "create",
            "--init",
            "--stop-signal",
            "SIGTERM",
            "--stop-timeout",
            "30",
            "--name",
            self.name(row["execution_id"], epoch),
            "--label",
            "v7.phase=P4",
            "--user",
            f"{os.getuid() or 65532}:{os.getgid() or 65532}",
            "--network",
            "host",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--cpus",
            "2",
            "--memory",
            "1g",
            "--memory-swap",
            "1g",
            "--pids-limit",
            "256",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=128m",
            "--mount",
            f"type=bind,src={root},dst=/run/work",
            "--mount",
            f"type=bind,src={config},dst=/run/config.json,readonly",
        ]
        for key, value in [
            ("action", row["action_id"]),
            ("execution", row["execution_id"]),
            ("digest", row["digest"]),
            ("attempt", row["attempt_id"]),
            ("epoch", str(epoch)),
        ]:
            args += ["--label", f"v7.{key}={value}"]
        for key, variable in (
            ("deployment", "V7_DEPLOYMENT_ID"),
            ("release", "V7_RELEASE_DIGEST"),
            ("test-run", "V7_TEST_RUN_ID"),
        ):
            if os.environ.get(variable):
                args += ["--label", f"v7.{key}={os.environ[variable]}"]
        engine = row.get("payload", {}).get("engine_package")
        if engine is not None:
            args += ["--label", f"v7.engine.strategy={engine['strategy']}"]
            args += ["--label", f"v7.engine.digest={engine['package_digest']}"]
        runtime_profile = row.get("runtime", {}).get("_engine_runtime_profile")
        if runtime_profile is not None:
            runtime_profile = validate_runtime_profile(runtime_profile)
            args += ["--label", f"v7.engine.profile={runtime_profile['profile_id']}"]
            args += ["--label", f"v7.engine.profile-digest={runtime_profile['profile_digest']}"]
        args.append(self.image)
        self.command(*args)
        return self.inspect(row["execution_id"], epoch)

    def start(self, container):
        self.command("start", container)

    def inspect_container(self, container):
        return Docker.inspect(self, container)

    def stop(self, container):
        """Stop an engine gracefully and confirm the container is no longer running."""
        try:
            self.command("stop", "--time", "30", container, timeout=35)
        except (OSError, subprocess.TimeoutExpired):
            # A timed-out or failed CLI call does not prove whether Docker
            # accepted the stop. Reconcile the original container identity.
            pass
        info = self.inspect_container(container)
        if info is not None and info["State"]["Running"]:
            self.command("kill", container)
            info = self.inspect_container(container)
        if info is not None and info["State"]["Running"]:
            raise OSError("Workflow engine did not stop")
        return info
