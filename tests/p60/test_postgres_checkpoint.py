from langgraph.checkpoint.postgres import PostgresSaver
from platform_agent_runtime.errors import RuntimeUnavailable
from platform_agent_runtime.langgraph_adapter import LangGraphRuntime
from platform_agent_runtime.profiles import LANGGRAPH_PROFILE_ID, profile

from tests.p60.test_runtime_semantics import ScriptedDecision, frame


def connection_string(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://")


def test_postgres_checkpoint_survives_adapter_reconstruction(postgres):
    decide = ScriptedDecision()
    runtime_ref = "agent-run-postgres"
    with PostgresSaver.from_conn_string(connection_string(postgres.url)) as saver:
        saver.setup()
        first = LangGraphRuntime(decide, saver)
        first.start(
            {"runtime_profile": profile(LANGGRAPH_PROFILE_ID)},
            frame(1, runtime_ref=runtime_ref),
        )
        proposal = first.advance(runtime_ref, frame(1, runtime_ref=runtime_ref), ())
        assert len(decide.calls) == 1

    with PostgresSaver.from_conn_string(connection_string(postgres.url)) as saver:
        recovered = LangGraphRuntime(decide, saver)
        assert recovered.inspect(runtime_ref).last_proposal_digest == proposal.digest
        assert recovered.advance(runtime_ref, frame(1, runtime_ref=runtime_ref), ()) == proposal
        assert len(decide.calls) == 1
        second = recovered.advance(runtime_ref, frame(2, runtime_ref=runtime_ref), ())
        assert second.frame_id == "frame-2"
        assert len(decide.calls) == 2


def test_closed_checkpoint_store_reports_unavailable_without_running_decision(postgres):
    decide = ScriptedDecision()
    runtime_ref = "agent-run-closed"
    with PostgresSaver.from_conn_string(connection_string(postgres.url)) as saver:
        saver.setup()
        runtime = LangGraphRuntime(decide, saver)
        runtime.start(
            {"runtime_profile": profile(LANGGRAPH_PROFILE_ID)},
            frame(1, runtime_ref=runtime_ref),
        )
    try:
        runtime.advance(runtime_ref, frame(1, runtime_ref=runtime_ref), ())
    except RuntimeUnavailable:
        pass
    else:
        raise AssertionError("A closed checkpoint connection must not look available")
    assert decide.calls == []
