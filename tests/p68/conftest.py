import json

import pytest
from platform_agent_runtime.profiles import profile
from platform_core.application.model_catalog import build_catalog

from tests.p3.conftest import service_databases  # noqa: F401
from tests.p3.harness import Cluster
from tests.p61.conftest import agent_setup  # noqa: F401
from tests.p62.conftest import model_profile
from tests.p63.conftest import database, model_agent_setup, owner, postgres  # noqa: F401


@pytest.fixture
def p6_browser_cluster(tmp_path, database, service_databases):  # noqa: F811
    runtime_path = tmp_path / "runtime-profile.json"
    model_path = tmp_path / "model-catalog.json"
    selected = model_profile()
    runtime_path.write_text(json.dumps(profile()), encoding="utf-8")
    model_path.write_text(
        json.dumps(build_catalog([selected], selected["profile_id"])), encoding="utf-8"
    )
    cluster = Cluster(tmp_path, database, service_databases)
    cluster.env.update(
        {
            "V7_AGENT_RUNTIME_PROFILE": str(runtime_path),
            "V7_MODEL_CATALOG": str(model_path),
        }
    )
    cluster.model_profile = selected
    try:
        cluster.start_services()
        yield cluster
    finally:
        cluster.close()
