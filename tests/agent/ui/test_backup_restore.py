import sqlalchemy as sa
from platform_agent_runtime.backup import create_backup, restore_backup
from platform_agent_runtime.checkpoint import CheckpointDatabase
from platform_agent_runtime.errors import RuntimeNotFound
from platform_agent_runtime.langgraph_adapter import LangGraphRuntime
from platform_agent_runtime.profiles import LANGGRAPH_PROFILE_ID, profile

from tests.agent.runtime.test_runtime_semantics import ScriptedDecision, frame


def test_agent_ledger_and_checkpoint_backup_restore_are_independent_and_exact(
    model_agent_setup, tmp_path
):
    database = model_agent_setup["database"]
    url = database.engine.url.render_as_string(hide_password=False)
    checkpoint = CheckpointDatabase(url)
    checkpoint.migrate()
    runtime_ref = "p68-backup-runtime"
    with checkpoint.saver() as saver:
        runtime = LangGraphRuntime(ScriptedDecision(), saver)
        runtime.start(
            {"runtime_profile": profile(LANGGRAPH_PROFILE_ID)},
            frame(1, runtime_ref=runtime_ref),
        )
        proposal = runtime.advance(runtime_ref, frame(1, runtime_ref=runtime_ref), ())

    destination = tmp_path / "agent-backup"
    manifest = create_backup(url, url, destination)
    assert manifest["platform"]["schema_version"] == "0026_worker_lane_leases"
    assert manifest["checkpoint"]["schema_version"] == "0001_langgraph_checkpoint"

    extra_project = model_agent_setup["control"].create_project(
        model_agent_setup["actor"], "p68-extra-project", "Created after backup"
    )
    with checkpoint.saver() as saver:
        extra = LangGraphRuntime(ScriptedDecision(), saver)
        extra.start(
            {"runtime_profile": profile(LANGGRAPH_PROFILE_ID)},
            frame(1, runtime_ref="p68-extra-runtime"),
        )

    restore_backup(url, url, destination, confirmed=True)
    with database.engine.connect() as connection:
        assert (
            connection.scalar(
                sa.text("SELECT count(*) FROM projects WHERE project_id=:project"),
                {"project": extra_project["project_id"]},
            )
            == 0
        )
    with checkpoint.saver() as saver:
        recovered = LangGraphRuntime(ScriptedDecision(), saver)
        assert recovered.inspect(runtime_ref).last_proposal_digest == proposal.digest
        try:
            recovered.inspect("p68-extra-runtime")
        except RuntimeNotFound:
            pass
        else:
            raise AssertionError("Checkpoint created after backup survived restore")


def test_restore_requires_explicit_confirmation(model_agent_setup, tmp_path):
    database = model_agent_setup["database"]
    url = database.engine.url.render_as_string(hide_password=False)
    CheckpointDatabase(url).migrate()
    destination = tmp_path / "agent-backup-confirm"
    create_backup(url, url, destination)
    try:
        restore_backup(url, url, destination)
    except RuntimeError as error:
        assert "explicit confirmation" in str(error)
    else:
        raise AssertionError("Restore proceeded without confirmation")
