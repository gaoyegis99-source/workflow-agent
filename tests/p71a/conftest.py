from pathlib import Path

import pytest

from tests.p2.conftest import database, owner, postgres  # noqa: F401
from tests.p3.conftest import service_databases  # noqa: F401
from tests.p3.harness import Cluster


@pytest.fixture
def cluster(tmp_path, database, service_databases):  # noqa: F811
    cluster = Cluster(tmp_path, database, service_databases)
    cluster.env["V7_TABULAR_OPERATOR_IMAGE"] = (
        open(".p7-runtime/tabular/image-id.txt", encoding="utf-8").read().strip()
    )
    cluster.env["V7_CWL_IMAGE"] = Path(".p4-runtime/cwl-image.txt").read_text().strip()
    cluster.env["V7_EXECUTION_CALLBACK_URL"] = cluster.env["V7_EXECUTION_URL"]
    try:
        cluster.start_services()
        yield cluster
    finally:
        cluster.close()
