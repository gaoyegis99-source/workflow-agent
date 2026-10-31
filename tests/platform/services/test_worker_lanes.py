import signal

import httpx
import pytest

from tests.platform.services.harness import Cluster, ProcessGroup


class FakeProcess:
    def __init__(self):
        self.returncode = None
        self.signals = []

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -signal.SIGKILL

    def terminate(self):
        self.returncode = -signal.SIGTERM

    def send_signal(self, signum):
        self.signals.append(signum)

    def wait(self, timeout=None):
        assert timeout is None or timeout >= 0
        return self.returncode


def test_worker_lane_group_reports_the_first_exited_lane():
    action = FakeProcess()
    knowledge = FakeProcess()
    group = ProcessGroup([action, knowledge])

    action.returncode = 0
    assert group.poll() == 0


def test_killing_worker_lane_group_stops_every_lane():
    processes = [FakeProcess() for _ in range(4)]
    group = ProcessGroup(processes)

    group.kill()
    group.wait(timeout=1)

    assert all(process.returncode == -signal.SIGKILL for process in processes)


def fake_cluster():
    cluster = object.__new__(Cluster)
    cluster.env = {"TEST": "worker-lanes"}
    cluster.ports = {}
    cluster.running = {}
    cluster.commands = []

    def start(command, environment):
        cluster.commands.append((command, environment))
        return FakeProcess()

    cluster.start = start
    return cluster


def test_harness_requires_explicit_multi_lane_helper():
    cluster = fake_cluster()

    with pytest.raises(KeyError):
        cluster.launch("platform-worker")

    group = cluster.launch_platform_worker_lanes(("action", "knowledge"))

    assert isinstance(group, ProcessGroup)
    assert cluster.running == {"platform-worker-lanes": group}
    assert [command[-1] for command, _ in cluster.commands] == ["action", "knowledge"]


def test_harness_default_starts_only_configured_baseline_lanes():
    cluster = fake_cluster()

    cluster.launch_platform_worker_lanes()

    assert [command[-1] for command, _ in cluster.commands] == [
        "package",
        "action",
        "acceptance",
    ]


def test_completion_wait_retries_transient_http_timeout_within_outer_deadline():
    cluster = fake_cluster()
    cluster.p = "/api/v1/projects/project"
    responses = iter(
        [
            httpx.ReadTimeout("transient read timeout"),
            {"status": "SUCCEEDED", "result": {"execution_id": "execution"}},
            {"execution_id": "execution", "state": "SUCCEEDED"},
        ]
    )

    def request(_method, _path):
        response = next(responses)
        if isinstance(response, Exception):
            raise response
        return response

    cluster.request = request

    assert cluster.completed_execution({"status_url": "/commands/command"}, timeout=1) == {
        "execution_id": "execution",
        "state": "SUCCEEDED",
    }


@pytest.mark.parametrize("lanes", [(), ("action", "action"), ("unknown",)])
def test_harness_rejects_invalid_explicit_lane_sets(lanes):
    with pytest.raises(ValueError):
        fake_cluster().launch_platform_worker_lanes(lanes)
