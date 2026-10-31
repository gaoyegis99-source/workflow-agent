"""Run the actual P3 Compose database bootstrap with isolated project resources."""

import os
import secrets
import subprocess
from uuid import uuid4

from platform_data.database import Database as DataDatabase
from platform_execution.database import Database as ExecutionDatabase
from platform_infrastructure.database import Database as PlatformDatabase

from scripts.maintenance.project_environment import docker_executable
from tests.pathing import SOURCE_ROOT
from tests.platform.services.harness import free_port


def test_clean_compose_database_bootstrap_and_explicit_migrations(tmp_path):
    name = "v7-p3-deploy-check-" + uuid4().hex[:10]
    port = free_port()
    env = os.environ | {"V7_P3_PG_PORT": str(port)}
    for key in [
        "V7_P3_PG_PASSWORD",
        "V7_PLATFORM_DB_PASSWORD",
        "V7_DATA_DB_PASSWORD",
        "V7_EXECUTION_DB_PASSWORD",
    ]:
        env[key] = secrets.token_hex(24)
    compose = [
        docker_executable(),
        "compose",
        "-p",
        name,
        "-f",
        str(SOURCE_ROOT / "deploy/platform/p3.compose.yaml"),
    ]
    compose_files = list(compose)
    try:
        try:
            subprocess.run(
                compose_files + ["up", "-d", "--wait"],
                env=env,
                check=True,
                capture_output=True,
                timeout=60,
            )
        except subprocess.CalledProcessError as error:
            # Docker hosts with exhausted predefined bridge pools cannot
            # allocate the compose-private network. Reuse Docker's existing
            # bridge only for this disposable fallback; database exposure is
            # still restricted to the selected loopback host port.
            stderr = (
                error.stderr.decode(errors="replace")
                if isinstance(error.stderr, bytes)
                else error.stderr
            )
            if "address pools have been fully subnetted" not in stderr:
                raise
            override = tmp_path / "p3-network-fallback.compose.yaml"
            override.write_text(
                "networks:\n  p3_private:\n    external: true\n    name: bridge\n",
                encoding="utf-8",
            )
            compose_files += ["-f", str(override)]
            subprocess.run(
                compose_files + ["up", "-d", "--wait"],
                env=env,
                check=True,
                capture_output=True,
                timeout=60,
            )
        for service, cls in [
            ("PLATFORM", PlatformDatabase),
            ("DATA", DataDatabase),
            ("EXECUTION", ExecutionDatabase),
        ]:
            url = (
                f"postgresql+psycopg://v7_{service.lower()}:{env[f'V7_{service}_DB_PASSWORD']}"
                f"@127.0.0.1:{port}/v7_{service.lower()}"
            )
            database = cls(url)
            try:
                database.migrate()
                database.migrate()
                database.check_schema()
            finally:
                database.close()
    finally:
        subprocess.run(
            compose_files + ["down", "--volumes", "--remove-orphans"],
            env=env,
            check=True,
            capture_output=True,
            timeout=30,
        )
