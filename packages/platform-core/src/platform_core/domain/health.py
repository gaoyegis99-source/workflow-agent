from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProcessHealth:
    """Process liveness only; does not assert readiness of future dependencies."""

    status: str = "ok"
