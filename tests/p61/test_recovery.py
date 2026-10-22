import pytest
from langgraph.checkpoint.memory import InMemorySaver
from platform_agent_runtime.errors import RuntimeCorrupt, RuntimeNotFound, RuntimeUnavailable
from platform_agent_runtime.kernel_worker import KernelWorker
from platform_agent_runtime.langgraph_adapter import LangGraphRuntime
from platform_core.application.agent_kernel import AgentCoordinator
from platform_core.domain.agent_runtime import RuntimeStatus

from tests.p61.test_kernel import PlanDecision, run_once, view


class PositionedRuntime:
    def __init__(self, revision=None, error=None):
        self.revision = revision
        self.error = error

    def inspect(self, runtime_ref):
        if self.error:
            raise self.error("checkpoint fault")
        return RuntimeStatus(runtime_ref, 1, self.revision, "WAITING_FRAME")


def wake(setup, suffix="1"):
    return AgentCoordinator(setup["database"]).register_event(
        setup["project_id"],
        setup["task"]["task_id"],
        source="EXECUTION",
        event_id=f"recovery-event-{suffix}",
        stream_id="recovery-execution",
        source_revision=int(suffix),
        kind="EXECUTION_CHANGED",
        payload={"state": "RUNNING"},
    )


@pytest.mark.parametrize(
    ("error", "revision", "expected_reason"),
    [
        (RuntimeNotFound, None, "MISSING"),
        (None, 0, "BEHIND"),
        (RuntimeCorrupt, None, "CORRUPT"),
    ],
)
def test_checkpoint_missing_behind_or_corrupt_rebuilds_new_epoch(
    agent_setup, error, revision, expected_reason
):
    run_once(agent_setup)
    assert wake(agent_setup)
    runtime = PositionedRuntime(revision=revision, error=error)
    assert KernelWorker(agent_setup["database"], runtime, "recovery-worker").process_one()
    saved = view(agent_setup)
    assert saved["epoch"] == 2
    assert saved["runtime_ref"].endswith("-e2")
    assert saved["blocked_reason"] == f"RECOVERED_FROM_LEDGER_{expected_reason}"
    assert saved["status"] == "READY"
    assert len(saved["proposals"]) == 1


def test_checkpoint_ahead_is_discarded_without_accepting_unledgered_proposal(agent_setup):
    runtime = PositionedRuntime(revision=1)
    assert KernelWorker(agent_setup["database"], runtime, "ahead-worker").process_one()
    saved = view(agent_setup)
    assert saved["epoch"] == 2
    assert saved["blocked_reason"] == "RECOVERED_FROM_LEDGER_AHEAD"
    assert saved["proposals"] == []


def test_checkpoint_unavailable_keeps_epoch_and_retries_same_runtime(agent_setup):
    runtime = PositionedRuntime(error=RuntimeUnavailable)
    assert KernelWorker(agent_setup["database"], runtime, "unavailable-worker").process_one()
    saved = view(agent_setup)
    assert saved["epoch"] == 1
    assert saved["status"] == "READY"
    assert saved["runtime_ref"].endswith("-e1")


def test_runtime_can_continue_after_ledger_recovery(agent_setup):
    run_once(agent_setup)
    assert wake(agent_setup)
    missing = PositionedRuntime(error=RuntimeNotFound)
    assert KernelWorker(agent_setup["database"], missing, "missing-worker").process_one()
    decision = PlanDecision()
    replacement = LangGraphRuntime(decision, InMemorySaver())
    assert KernelWorker(agent_setup["database"], replacement, "replacement-worker").process_one()
    saved = view(agent_setup)
    assert saved["epoch"] == 2
    assert len(saved["proposals"]) == 2
    assert saved["proposals"][-1]["epoch"] == 2
    assert len(decision.calls) == 1
