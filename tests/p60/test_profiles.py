import copy

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from platform_agent_runtime.langgraph_adapter import LangGraphRuntime
from platform_agent_runtime.profiles import (
    LANGGRAPH_PROFILE_ID,
    PLAIN_PROFILE_ID,
    SELECTED_PROFILE_ID,
    diagnostic,
    profile,
    validate,
)

from tests.p60.test_runtime_semantics import ScriptedDecision


def test_profiles_are_exact_and_langgraph_is_selected():
    plain = profile(PLAIN_PROFILE_ID)
    graph = profile(LANGGRAPH_PROFILE_ID)
    assert SELECTED_PROFILE_ID == LANGGRAPH_PROFILE_ID
    assert plain["adapter"] == "PLAIN_LOOP"
    assert graph["adapter"] == "LANGGRAPH"
    assert graph["framework"] == "langgraph"
    assert graph["framework_version"] == "1.2.15"
    assert graph["checkpoint_kind"] == "POSTGRES"
    assert diagnostic(graph)["profile_digest"] == graph["profile_digest"]
    runtime = LangGraphRuntime(ScriptedDecision(), InMemorySaver())
    thread_id = runtime._config("agent-run-1")["configurable"]["thread_id"]
    assert thread_id == f"p6-runtime-v1:{graph['profile_digest']}:agent-run-1"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(max_model_calls_per_advance=2),
        lambda value: value.update(profile_digest="sha256:" + "0" * 64),
        lambda value: value.update(control_mode="AUTOMATIC_TOOLS"),
        lambda value: value.update(extra=True),
    ],
)
def test_profile_tampering_is_rejected(mutation):
    value = copy.deepcopy(profile())
    mutation(value)
    with pytest.raises((ValueError, TypeError)):
        validate(value)
