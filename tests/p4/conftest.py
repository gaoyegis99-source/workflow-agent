import sys

import pytest
from platform_contracts.engine_runtime import ON_PROFILE_ID

from tests.p2.conftest import database, owner, postgres  # noqa: F401
from tests.p3.conftest import service_databases  # noqa: F401


@pytest.fixture
def p4_cluster(tmp_path, database, service_databases):  # noqa: F811
    from pathlib import Path

    from tests.p3.harness import Cluster, cleanup_test_containers

    cluster = Cluster(tmp_path, database, service_databases)
    cluster.env["V7_CWL_IMAGE"] = Path(".p4-runtime/cwl-image.txt").read_text().strip()
    cluster.env["V7_EXECUTION_CALLBACK_URL"] = cluster.env["V7_EXECUTION_URL"]
    try:
        cluster.start_services()
        yield cluster
    finally:
        cluster.close()
        cleanup_test_containers(cluster.test_run_id)


@pytest.fixture
def p4_chaining_on_cluster(tmp_path, database, service_databases):  # noqa: F811
    from pathlib import Path

    from tests.p3.harness import Cluster, cleanup_test_containers

    cluster = Cluster(tmp_path, database, service_databases)
    cluster.env["V7_CWL_IMAGE"] = Path(".p4-runtime/cwl-image.txt").read_text().strip()
    cluster.env["V7_EXECUTION_CALLBACK_URL"] = cluster.env["V7_EXECUTION_URL"]
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
