"""Run the P4.2.1 spatial workflow using only installed wheels and fixed images."""

import hashlib
import json
import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path

import sqlalchemy as sa
from platform_data.database import Database as DataDatabase
from platform_execution.database import Database as ExecutionDatabase
from platform_infrastructure.database import Database

P4_RUNTIME_ROOT = (
    Path(os.environ.get("V7_P4_RUNTIME_ROOT", "runtime/work/p4")).expanduser().resolve()
)

from tests.platform.database.processes import eventually  # noqa: E402
from tests.platform.services.harness import Cluster, cleanup_test_containers  # noqa: E402


def main():
    database = Database(os.environ["V7_DATABASE_URL"])
    database.migrate()
    database.check_schema()
    admin = sa.create_engine(os.environ["V7_DATABASE_URL"], isolation_level="AUTOCOMMIT")
    services = []
    root = Path("installed-p42-runtime").resolve()
    try:
        for suffix, cls in (("data", DataDatabase), ("execution", ExecutionDatabase)):
            name = "v7_p42_" + suffix
            password = secrets.token_hex(24)
            with admin.connect() as connection:
                connection.exec_driver_sql(f"CREATE ROLE {name} LOGIN PASSWORD '{password}'")
                connection.exec_driver_sql(f"CREATE DATABASE {name} OWNER {name}")
                connection.exec_driver_sql(f"REVOKE ALL ON DATABASE {name} FROM PUBLIC")
            url = admin.url.set(username=name, password=password, database=name)
            service = cls(url.render_as_string(hide_password=False))
            service.migrate()
            service.migrate()
            service.check_schema()
            services.append(service)
        principal = database.create_principal("installed-p42-owner")
        token = database.issue_token(principal)["token"]
        root.mkdir()
        cluster = Cluster(root, database, services)
        images = json.loads((P4_RUNTIME_ROOT / "spatial/images.json").read_text())["images"]
        cluster.env |= {
            "V7_CWL_IMAGE": (P4_RUNTIME_ROOT / "cwl-image.txt").read_text().strip(),
            "V7_EXECUTION_CALLBACK_URL": cluster.workflow_callback_url,
            "V7_SPATIAL_QGIS_IMAGE_ID": images["qgis"],
            "V7_SPATIAL_NATIVE_IMAGE_ID": images["native"],
            "V7_DATA_SPATIAL_WORK_ROOT": str(root / "spatial-imports"),
        }
        try:
            cluster.start_services()
            cluster.launch("data-spatial-worker")
            cluster.client.headers["Authorization"] = "Bearer " + token
            project = cluster.request("POST", "/api/v1/projects", {"name": "Installed spatial"})
            cluster.project = project["project_id"]
            cluster.p = "/api/v1/projects/" + cluster.project
            content = Path("tests/spatial/native/fixtures/points-utm.gpkg").read_bytes()
            upload = cluster.request(
                "POST",
                cluster.p + "/uploads",
                {
                    "filename": "points-utm.gpkg",
                    "format": "geopackage",
                    "size": len(content),
                    "digest": "sha256:" + hashlib.sha256(content).hexdigest(),
                },
            )
            path = cluster.p + "/uploads/" + upload["upload_id"]
            cluster.put_content(path + "/content", content)
            cluster.request(
                "POST", path + "/finalize", {"kind": "SPATIAL", "profile": "strict-spatial-v1"}
            )
            uploaded = eventually(
                lambda: (
                    value if (value := cluster.request("GET", path))["status"] == "READY" else None
                ),
                timeout=30,
            )
            ref = uploaded["asset"]
            asset = cluster.request("GET", cluster.p + f"/assets/{ref['asset_id']}/versions/1")
            selection = {
                "asset": ref,
                "representation_id": "original",
                "representation_digest": asset["manifest"]["representations"][0]["digest"],
                "selector": {"kind": "VECTOR_LAYER", "layer_id": "l0"},
                "semantic_revision": None,
            }
            task = cluster.request(
                "POST",
                cluster.p + "/tasks",
                {
                    "execution_mode": "DIRECT",
                    "spec": {"goal": "Installed QGIS buffer and summary", "requirements": []},
                    "budget": {"execution_count": 2, "max_in_flight": 2},
                },
            )
            cluster.task = task["task_id"]
            definition = json.loads(
                Path("examples/workflows/qgis-buffer-summary-v1.json").read_text()
            )
            queued = cluster.request(
                "POST", cluster.p + "/workflow-packages", definition, key="installed-package"
            )
            cluster.launch_platform_worker_lanes()
            package = eventually(
                lambda: (
                    value
                    if (value := cluster.request("GET", queued["status_url"]))["status"] == "READY"
                    else None
                ),
                timeout=40,
            )
            workflow_target = {"kind": "WORKFLOW", "package_ref": package["package_ref"]}
            cluster.request(
                "POST",
                cluster.p + "/grants",
                {
                    "task_id": cluster.task,
                    "targets": [workflow_target, *definition["implementations"].values()],
                    "resources": [ref],
                    "derived_outputs": True,
                    "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                },
            )
            command = cluster.request(
                "POST",
                cluster.p + "/executions",
                {
                    "task_id": cluster.task,
                    "expected_spec_revision": 1,
                    "target": workflow_target,
                    "inputs": {
                        "vector": selection,
                        "buffer_parameters": {
                            "distance": 100,
                            "unit": "metre",
                            "output_crs": "EPSG:32650",
                            "segments": 8,
                        },
                        "summary_parameters": {"layer_id": "l0"},
                    },
                    "max_node_executions": 2,
                },
            )
            cluster.launch("execution-worker")
            execution = cluster.completed_execution(command, timeout=180)
            assert execution["state"] == "SUCCEEDED", execution
            assert len(execution["runtime"]["publication_receipt"]["assets"]) == 2
            task_view = cluster.request("GET", cluster.p + f"/tasks/{cluster.task}")
            assert task_view["lifecycle"] == "OPEN"
            assert cluster.client.get("/").status_code == 200
        finally:
            cluster.close()
    except BaseException:
        import sys

        for log in root.glob("process-*.log"):
            print(log.name + "\n" + log.read_text()[-6000:], file=sys.stderr)
        raise
    finally:
        if "cluster" in locals():
            cleanup_test_containers(cluster.test_run_id)
        for service in services:
            service.close()
        database.close()
        admin.dispose()


if __name__ == "__main__":
    main()
