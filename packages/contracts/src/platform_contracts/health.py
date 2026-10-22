"""Small process-health signals shared by the long-running V7 workers."""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def touch_worker_heartbeat() -> None:
    """Record progress of a worker's main loop for its container healthcheck."""

    raw_path = os.environ.get("V7_WORKER_HEARTBEAT_FILE")
    if not raw_path:
        return
    try:
        Path(raw_path).touch(exist_ok=True)
    except OSError as error:
        logger.warning("Worker heartbeat unavailable category=%s", type(error).__name__)
