import pytest

from scripts.lib.runtime_layout import P4_RUNTIME_ROOT, P5_RUNTIME_ROOT, P7_RUNTIME_ROOT
from tests.operators.tabular.core.conftest import Cluster
from tests.platform.database.conftest import database, owner, postgres  # noqa: F401
from tests.platform.services.conftest import service_databases  # noqa: F401


@pytest.fixture
def cluster(tmp_path, database, service_databases, monkeypatch):  # noqa: F811
    value = Cluster(tmp_path, database, service_databases)
    checker_path = (P5_RUNTIME_ROOT / "checker-catalog.json").resolve()
    value.configure_checker_catalog(checker_path)
    value.env |= {
        "V7_TABULAR_OPERATOR_IMAGE": (P7_RUNTIME_ROOT / "tabular/image-id.txt")
        .read_text(encoding="utf-8")
        .strip(),
        "V7_CWL_IMAGE": (P4_RUNTIME_ROOT / "cwl-image.txt").read_text(encoding="utf-8").strip(),
    }
    value.env["V7_EXECUTION_CALLBACK_URL"] = value.workflow_callback_url
    for name in ("V7_CHECKER_CATALOG", "V7_CHECKER_IMAGE", "V7_GDAL_CHECKER_IMAGE"):
        monkeypatch.setenv(name, value.env[name])
    try:
        value.start_services()
        yield value
    finally:
        value.close()
