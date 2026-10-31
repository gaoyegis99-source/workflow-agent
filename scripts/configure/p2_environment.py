"""Disposable PostgreSQL 17 used only by verification; never reads a production DSN."""

import atexit
import json
import os
import re
import secrets
import signal
import socket
import subprocess

# Direct Python invocations must use the same project Docker boundary as bin/ops.
import sys as _maintenance_sys  # noqa: E402
import threading
import time
from pathlib import Path as _MaintenancePath  # noqa: E402
from uuid import uuid4

import psycopg

_maintenance_source = next(
    parent
    for parent in _MaintenancePath(__file__).resolve().parents
    if (parent / "scripts/maintenance/project_environment.py").is_file()
)
_maintenance_sys.path.insert(0, str(_maintenance_source))
from scripts.maintenance.project_environment import docker_executable  # noqa: E402

POSTGRES_IMAGE = "postgres@sha256:3645570cccdfa447589da9f57dd740faa29b30938e861289a5574b6ca6b03826"
DOCKER_COMMAND_TIMEOUT_SECONDS = 180


def _published_docker_ports():
    result = subprocess.run(
        [docker_executable(), "ps", "--format", "{{.Ports}}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if result.returncode:
        return set()
    return {
        int(value)
        for value in re.findall(r"(?:127\.0\.0\.1|0\.0\.0\.0|::):([0-9]+)->", result.stdout)
    }


def _free_port():
    published = _published_docker_ports()
    for _ in range(20):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        if port not in published:
            return port
    raise RuntimeError("could not allocate a host port not published by Docker")


class PostgreSQL:
    def __init__(self):
        self.name = "v7-p2-check-" + uuid4().hex[:10]
        self.test_run_id = "p2-" + uuid4().hex
        self.password = secrets.token_hex(24)
        self._cleaned = False
        self._previous_sigterm = None
        self.port = _free_port()

    def docker(self, *args, check=True):
        return subprocess.run(
            [docker_executable(), *args],
            capture_output=True,
            text=True,
            check=check,
            timeout=DOCKER_COMMAND_TIMEOUT_SECONDS,
            env=os.environ | {"POSTGRES_PASSWORD": self.password},
        ).stdout.strip()

    def __enter__(self):
        try:
            self._install_abort_cleanup()
            self.network = self.name
            try:
                self.docker(
                    "network",
                    "create",
                    "--label",
                    "v7.phase=P2",
                    "--label",
                    f"v7.test-run={self.test_run_id}",
                    self.name,
                )
            except subprocess.CalledProcessError as exc:
                # Busy development hosts can exhaust Docker's predefined /16
                # pools even though the shared bridge remains available.  The
                # container and volume are still unique; host exposure remains
                # restricted to the unique 127.0.0.1 port.
                if "address pools have been fully subnetted" not in exc.stderr:
                    raise
                self.network = "bridge"
            self.docker(
                "volume",
                "create",
                "--label",
                "v7.phase=P2",
                "--label",
                f"v7.test-run={self.test_run_id}",
                self.name,
            )
            self.docker(
                "run",
                "-d",
                "--name",
                self.name,
                "--label",
                "v7.phase=P2",
                "--label",
                f"v7.test-run={self.test_run_id}",
                "--network",
                self.network,
                "--mount",
                f"type=volume,src={self.name},dst=/var/lib/postgresql/data",
                "--memory",
                "512m",
                "--cpus",
                "2",
                "-p",
                f"127.0.0.1:{self.port}:5432",
                "-e",
                "POSTGRES_PASSWORD",
                "-e",
                "POSTGRES_USER=v7",
                "-e",
                "POSTGRES_DB=v7_p2",
                POSTGRES_IMAGE,
            )
            deadline = time.monotonic() + 30
            while True:
                ready = subprocess.run(
                    [
                        docker_executable(),
                        "exec",
                        self.name,
                        "pg_isready",
                        "-h",
                        "127.0.0.1",
                        "-U",
                        "v7",
                        "-d",
                        "v7_p2",
                    ],
                    capture_output=True,
                    timeout=5,
                )
                if ready.returncode == 0:
                    break
                if time.monotonic() > deadline:
                    raise TimeoutError("P2 PostgreSQL did not start")
                time.sleep(0.2)
            info = json.loads(self.docker("inspect", self.name))[0]
            port = info["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostPort"]
            self.url = f"postgresql+psycopg://v7:{self.password}@127.0.0.1:{port}/v7_p2"
            # Container-local readiness can become visible a moment before the
            # rootless host port forward is ready.  Verify the same host path
            # used by the test services so callers never inherit that race.
            deadline = time.monotonic() + 30
            while True:
                try:
                    with psycopg.connect(
                        host="127.0.0.1",
                        port=int(port),
                        dbname="v7_p2",
                        user="v7",
                        password=self.password,
                        connect_timeout=1,
                    ) as connection:
                        connection.execute("SELECT 1")
                    break
                except psycopg.OperationalError:
                    if time.monotonic() > deadline:
                        raise TimeoutError("P2 PostgreSQL host port did not become ready") from None
                    time.sleep(0.2)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        if self._cleaned:
            return
        self._cleaned = True
        self.docker("rm", "-f", self.name, check=False)
        self.docker("volume", "rm", self.name, check=False)
        if getattr(self, "network", None) == self.name:
            self.docker("network", "rm", self.name, check=False)
        if self._previous_sigterm is not None:
            signal.signal(signal.SIGTERM, self._previous_sigterm)

    def _install_abort_cleanup(self):
        """Make an interrupted pytest run remove only this database fixture."""
        if threading.current_thread() is not threading.main_thread():
            return
        self._previous_sigterm = signal.getsignal(signal.SIGTERM)

        def on_sigterm(signum, frame):
            self.__exit__(None, None, None)
            previous = self._previous_sigterm
            if callable(previous):
                previous(signum, frame)
            raise SystemExit(128 + signum)

        signal.signal(signal.SIGTERM, on_sigterm)
        atexit.register(self.__exit__, None, None, None)
