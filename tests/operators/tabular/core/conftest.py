import pytest

from scripts.lib.runtime_layout import P4_RUNTIME_ROOT, P7_RUNTIME_ROOT
from tests.platform.database.conftest import database, owner, postgres  # noqa: F401
from tests.platform.services.conftest import service_databases  # noqa: F401
from tests.platform.services.harness import Cluster


@pytest.fixture
def cluster(tmp_path, database, service_databases):  # noqa: F811
    cluster = Cluster(tmp_path, database, service_databases)
    cluster.env["V7_TABULAR_OPERATOR_IMAGE"] = (
        (P7_RUNTIME_ROOT / "tabular/image-id.txt").read_text(encoding="utf-8").strip()
    )
    cluster.env["V7_CWL_IMAGE"] = (P4_RUNTIME_ROOT / "cwl-image.txt").read_text().strip()
    cluster.env["V7_EXECUTION_CALLBACK_URL"] = cluster.workflow_callback_url
    try:
        cluster.start_services()
        yield cluster
    finally:
        cluster.close()
