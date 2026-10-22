from platform_infrastructure import worker as worker_module
from platform_infrastructure.worker import (
    IDLE_POLL_INITIAL_SECONDS,
    IDLE_POLL_MAX_SECONDS,
    Worker,
)


class IterationStop:
    def __init__(self, iterations):
        self.iterations = iterations
        self.checks = 0
        self.waits = []

    def is_set(self):
        self.checks += 1
        return self.checks > self.iterations

    def wait(self, seconds):
        self.waits.append(seconds)
        return False


def worker_with_results(results):
    worker = object.__new__(Worker)
    worker.packages = None
    worker.completion = None
    worker.lifecycle = None
    outcomes = iter(results)
    worker.process_one = lambda: next(outcomes)
    return worker


def test_idle_worker_uses_bounded_exponential_backoff():
    stop = IterationStop(7)
    worker_with_results([False] * 7).run(stop)

    assert stop.waits == [
        IDLE_POLL_INITIAL_SECONDS,
        0.4,
        0.8,
        1.6,
        3.2,
        IDLE_POLL_MAX_SECONDS,
        IDLE_POLL_MAX_SECONDS,
    ]


def test_idle_worker_refreshes_health_heartbeat(monkeypatch):
    heartbeats = []
    monkeypatch.setattr(worker_module, "touch_worker_heartbeat", lambda: heartbeats.append(True))
    stop = IterationStop(3)

    worker_with_results([False] * 3).run(stop)

    assert len(heartbeats) == 3


def test_successful_work_resets_idle_backoff():
    stop = IterationStop(4)
    worker_with_results([False, False, True, False]).run(stop)

    assert stop.waits == [0.2, 0.4, 0.2]
