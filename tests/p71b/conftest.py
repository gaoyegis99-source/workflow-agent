import json
from pathlib import Path

import pytest

from tests.p2.conftest import database, owner, postgres  # noqa: F401
from tests.p3.conftest import service_databases  # noqa: F401
from tests.p71a.conftest import Cluster


@pytest.fixture
def cluster(tmp_path, database, service_databases, monkeypatch):  # noqa: F811
    value = Cluster(tmp_path, database, service_databases)
    checker_path = Path(".p5-runtime/checker-catalog.json").resolve()
    checker = json.loads(checker_path.read_text(encoding="utf-8"))
    value.env |= {
        "V7_CHECKER_CATALOG": str(checker_path),
        "V7_CHECKER_IMAGE": checker["images"]["CHECKER"],
        "V7_GDAL_CHECKER_IMAGE": checker["images"]["GDAL_CHECKER"],
        "V7_TABULAR_OPERATOR_IMAGE": Path(".p7-runtime/tabular/image-id.txt")
        .read_text(encoding="utf-8")
        .strip(),
        "V7_CWL_IMAGE": Path(".p4-runtime/cwl-image.txt").read_text(encoding="utf-8").strip(),
    }
    value.env["V7_EXECUTION_CALLBACK_URL"] = value.env["V7_EXECUTION_URL"]
    for name in ("V7_CHECKER_CATALOG", "V7_CHECKER_IMAGE", "V7_GDAL_CHECKER_IMAGE"):
        monkeypatch.setenv(name, value.env[name])
    try:
        value.start_services()
        yield value
    finally:
        value.close()
