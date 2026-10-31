"""Small process-health signals shared by the long-running V7 workers."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def touch_worker_heartbeat(path: Path | None) -> None:
    """Record a composed worker health location without reading process state."""

    if path is None:
        return
    try:
        path.touch(exist_ok=True)
    except OSError as error:
        logger.warning("Worker heartbeat unavailable category=%s", type(error).__name__)
