"""Process-lock and lease-heartbeat guard for one claimed execution."""

from __future__ import annotations

import fcntl
import logging
import threading
from pathlib import Path

from .execution_runtime import LeaseLostError


class ExecutionLeaseGuard:
    def __init__(self, repository, row, root: Path, lease_seconds: float, heartbeat_seconds: float):
        self.repository = repository
        self.row = row
        self.root = Path(root)
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.delay = 0.2
        self.acquired = False
        self.lost = threading.Event()
        self._stop = threading.Event()
        self._lock = None
        self._thread = None

    def _heartbeat(self) -> None:
        while not self._stop.wait(self.heartbeat_seconds):
            try:
                if not self.repository.renew(self.row, self.lease_seconds):
                    self.lost.set()
                    return
            except Exception:
                logging.warning(
                    "Execution lease renewal unavailable action=%s generation=%s",
                    self.row["action_id"],
                    self.row["generation"],
                )
                self.lost.set()
                return

    def assert_active(self) -> None:
        """Fence every new external side effect against the durable lease."""

        if not self.acquired or self.lost.is_set():
            raise LeaseLostError("EXECUTION_LEASE_LOST")
        try:
            current = self.repository.claim_is_current(self.row)
        except Exception as error:
            self.lost.set()
            raise LeaseLostError("EXECUTION_LEASE_UNAVAILABLE") from error
        if not current:
            self.lost.set()
            raise LeaseLostError("EXECUTION_LEASE_LOST")

    def __enter__(self):
        self._lock = (self.root / "runtime.lock").open("a")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.acquired = True
            self._thread = threading.Thread(target=self._heartbeat, daemon=True)
            self._thread.start()
        except BlockingIOError:
            self.acquired = False
        return self

    def __exit__(self, exc_type, exc, traceback):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.heartbeat_seconds + 4)
        if self._lock is not None:
            self._lock.close()
        self.repository.release(self.row, self.delay)
        return False
