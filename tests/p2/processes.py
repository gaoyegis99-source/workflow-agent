"""Owned subprocesses and bounded condition waits for P2 fault tests."""

import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

from .peer_client import PeerClient

HERE = Path(__file__).parent


def eventually(condition, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = condition()
        if result:
            return result
        time.sleep(0.05)
    raise AssertionError("Condition did not become true before deadline")


class Processes:
    def __init__(self, directory, database_url, *, peer_directory=None):
        self.directory, self.database_url = directory, database_url
        self.peer_directory = peer_directory or directory
        self.children = []
        self.logs = []
        self.token = secrets.token_urlsafe(32)

    def start(self, args, extra=None):
        log = (self.directory / f"process-{len(self.children)}.log").open("wb")
        self.logs.append(log)
        process = subprocess.Popen(
            [str(part) for part in args],
            env=os.environ
            | {"V7_DATABASE_URL": self.database_url, "P2_PEER_TOKEN": self.token}
            | (extra or {}),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        self.children.append(process)
        return process

    def peer(self):
        marker = self.directory / f"peer-{len(self.children)}.json"
        process = self.start(
            [
                sys.executable,
                HERE / "peer.py",
                "--database",
                self.peer_directory / "peer.sqlite",
                "--ready-file",
                marker,
            ]
        )
        eventually(marker.exists, timeout=30)
        port = json.loads(marker.read_text())["port"]
        self.client = PeerClient(f"http://127.0.0.1:{port}", self.token)
        return process, self.client

    def worker(self, *, once=False):
        return self.start(
            [sys.executable, HERE / "driver.py", "worker", *(["--once"] if once else [])],
            {"P2_PEER_URL": self.client.base},
        )

    def api(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        process = self.start([sys.executable, "-m", "platform_api", "--port", str(port)])
        client = httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=5)

        def ready():
            try:
                return client.get("/api/v1/ready").status_code == 200
            except httpx.TransportError:
                return False

        eventually(ready)
        return process, client

    @staticmethod
    def kill(process):
        process.kill()
        process.wait(timeout=8)

    def close(self):
        for process in reversed(self.children):
            if process.poll() is None:
                process.send_signal(signal.SIGCONT)
                process.terminate()
                try:
                    process.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    self.kill(process)
        for log in self.logs:
            log.close()
