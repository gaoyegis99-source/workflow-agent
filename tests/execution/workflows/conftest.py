import sys

import pytest
from platform_contracts.engine_runtime import ON_PROFILE_ID

from scripts.lib.runtime_layout import P4_RUNTIME_ROOT
from tests.platform.database.conftest import database, owner, postgres  # noqa: F401
from tests.platform.services.conftest import service_databases  # noqa: F401


@pytest.fixture
def p4_cluster(tmp_path, database, service_databases):  # noqa: F811

    from tests.platform.services.harness import Cluster, cleanup_test_containers

    cluster = Cluster(tmp_path, database, service_databases)
    cluster.env["V7_CWL_IMAGE"] = (P4_RUNTIME_ROOT / "cwl-image.txt").read_text().strip()
    cluster.env["V7_EXECUTION_CALLBACK_URL"] = cluster.workflow_callback_url
    try:
        cluster.start_services()
        yield cluster
    finally:
        cluster.close()
        cleanup_test_containers(cluster.test_run_id)


@pytest.fixture
def p4_chaining_on_cluster(tmp_path, database, service_databases):  # noqa: F811

    from tests.platform.services.harness import Cluster, cleanup_test_containers

    cluster = Cluster(tmp_path, database, service_databases)
    cluster.env["V7_CWL_IMAGE"] = (P4_RUNTIME_ROOT / "cwl-image.txt").read_text().strip()
    cluster.env["V7_EXECUTION_CALLBACK_URL"] = cluster.workflow_callback_url
    execution_command = [
        sys.executable,
        "-m",
        "tests.performance.profile_execution_service",
        "--profile",
        ON_PROFILE_ID,
    ]
    try:
        cluster.start_services(execution_command=execution_command)
        yield cluster
    finally:
        cluster.close()
        cleanup_test_containers(cluster.test_run_id)
