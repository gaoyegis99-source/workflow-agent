import pytest
from platform_contracts import DeploymentContext, ServiceEndpoint
from platform_infrastructure import worker as worker_module
from platform_infrastructure.services import execution_client
from platform_infrastructure.worker import (
    IDLE_POLL_INITIAL_SECONDS,
    IDLE_POLL_MAX_SECONDS,
    Worker,
    WorkerLane,
    parse_args,
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
    worker.lane = WorkerLane.ACTION
    worker.packages = None
    worker.completion = None
    worker.lifecycle = None
    worker.heartbeat_file = None
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
    monkeypatch.setattr(
        worker_module,
        "touch_worker_heartbeat",
        lambda _path: heartbeats.append(True),
    )
    stop = IterationStop(3)

    worker_with_results([False] * 3).run(stop)

    assert len(heartbeats) == 3


def test_successful_work_resets_idle_backoff():
    stop = IterationStop(4)
    worker_with_results([False, False, True, False]).run(stop)

    assert stop.waits == [0.2, 0.4, 0.2]


class Processor:
    def __init__(self, result=False):
        self.result = result
        self.calls = 0

    def process_one(self):
        self.calls += 1
        return self.result


class CompletionProcessor(Processor):
    pass


def lane_worker(lane):
    worker = object.__new__(Worker)
    worker.lane = lane
    worker.packages = Processor(True)
    worker.completion = CompletionProcessor(True)
    worker.lifecycle = Processor(True)
    worker.heartbeat_file = None
    worker._process_action = Processor(True).process_one
    return worker


def test_each_lane_only_invokes_its_owned_processor():
    expected = {
        WorkerLane.PACKAGE: "packages",
        WorkerLane.ACCEPTANCE: "completion",
        WorkerLane.KNOWLEDGE: "lifecycle",
    }
    for lane, selected in expected.items():
        worker = lane_worker(lane)

        assert worker.process_one() is True
        assert worker.packages.calls == (1 if selected == "packages" else 0)
        assert worker.completion.calls == (1 if selected == "completion" else 0)
        assert worker.lifecycle.calls == (1 if selected == "lifecycle" else 0)


def test_action_lane_does_not_invoke_other_processors():
    worker = lane_worker(WorkerLane.ACTION)

    assert worker.process_one() is True
    assert worker.packages.calls == 0
    assert worker.completion.calls == 0
    assert worker.lifecycle.calls == 0


def test_action_lane_does_not_issue_an_effect_after_its_claim_is_lost():
    class Dispatcher:
        def __init__(self):
            self.owned_calls = 0
            self.uncertain_calls = 0

        @staticmethod
        def claim(owner, seconds):
            return {
                "event_id": "event",
                "owner": owner,
                "generation": 1,
                "kind": "ACTION",
                "payload": {"action_id": "action"},
            }

        @staticmethod
        def renew(claim, seconds):
            return True

        def owned(self, claim):
            self.owned_calls += 1
            return self.owned_calls == 1

        @staticmethod
        def prepare(claim, **unused):
            return {"method": "submit", "action_id": "action", "request": {"safe": True}}

        def uncertain(self, claim):
            self.uncertain_calls += 1

    class Execution:
        submitted = []

        def submit(self, *args):
            self.submitted.append(args)

    worker = object.__new__(Worker)
    worker.dispatcher = Dispatcher()
    worker.execution = Execution()
    worker.owner = "lane"
    worker.lease_seconds, worker.heartbeat_seconds = 30, 10

    assert worker._process_action() is True
    assert worker.execution.submitted == []
    assert worker.dispatcher.uncertain_calls == 0


def test_lane_backoff_is_not_affected_by_other_processors():
    worker = lane_worker(WorkerLane.KNOWLEDGE)
    worker.lifecycle.result = False
    stop = IterationStop(3)

    worker.run(stop)

    assert worker.lifecycle.calls == 3
    assert worker.packages.calls == 0
    assert worker.completion.calls == 0
    assert stop.waits == [0.2, 0.4, 0.8]


def test_once_requires_an_explicit_lane():
    with pytest.raises(SystemExit):
        parse_args(["--once"])


def test_programmatic_worker_requires_an_explicit_lane():
    with pytest.raises(TypeError, match="lane"):
        Worker(None)


@pytest.mark.parametrize("lane", [value.value for value in WorkerLane])
def test_each_lane_is_a_supported_cli_target(lane):
    args = parse_args(["--lane", lane, "--once"])

    assert args.lane == lane
    assert args.once is True


def test_execution_lane_does_not_require_data_service_configuration(monkeypatch):
    client = execution_client(
        ServiceEndpoint("http://execution.internal", "x" * 32),
        deployment=DeploymentContext(),
    )
    try:
        assert client.url == "http://execution.internal"
    finally:
        client.close()
