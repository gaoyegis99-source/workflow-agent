import pytest
from platform_agent_runtime.profiles import profile
from platform_core.application.control import ControlService

from tests.p2.conftest import database, owner, postgres  # noqa: F401


@pytest.fixture
def agent_setup(database, owner):  # noqa: F811
    actor, token = owner
    control = ControlService(database, agent_runtime_profile=profile())
    project = control.create_project(actor, "p61-project", "P6.1")
    task = control.create_task(
        actor,
        project["project_id"],
        "p61-task",
        {
            "execution_mode": "AGENT",
            "spec": {"goal": "Build a deterministic plan", "requirements": []},
            "budget": {"execution_count": 2, "max_in_flight": 1},
        },
    )
    with database.transaction() as uow:
        run = uow.agents.run_by_task(task["task_id"], required=True)
    return {
        "database": database,
        "actor": actor,
        "token": token,
        "control": control,
        "project_id": project["project_id"],
        "task": task,
        "agent_run_id": run["agent_run_id"],
    }
