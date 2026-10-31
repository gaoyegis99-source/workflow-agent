"""Run the P4.1 workflow closure using only installed wheels and fixed images."""

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

# Package validation runs on a dedicated durable worker lane.  Distribution
# verification can follow the complete P2 suite, so shared PostgreSQL and the
# rootless runtime may need longer than the old 30-second boundary to settle.
# The worker health check below keeps this bounded wait fail-fast on real exits.
PACKAGE_READY_TIMEOUT = 90


def workflow(targets):
    parameters = {
        "type": "record",
        "name": "AnalysisParameters",
        "fields": [
            {"name": "groups", "type": {"type": "array", "items": "string"}},
            {"name": "start", "type": "string"},
            {"name": "end", "type": "string"},
            {"name": "frequency", "type": "string"},
            {"name": "min_samples", "type": "int"},
        ],
    }
    outputs = {}
    steps = {}
    implementations = {}
    for name, target in zip(("complete", "trend"), targets, strict=True):
        implementations[name] = target
        steps[name] = {
            "run": name,
            "in": {"observations": "observations", "parameters": "parameters"},
            "out": ["result", "table", "method"],
        }
        for part in ("result", "table", "method"):
            outputs[name + "_" + part] = {
                "type": "File",
                "outputSource": name + "/" + part,
            }
    return {
        "name": "Installed P4 workflow",
        "entrypoint": "main",
        "profile": "v7-cwl-p4-v1",
        "documents": {
            "main": {
                "cwlVersion": "v1.2",
                "class": "Workflow",
                "inputs": {"observations": "File", "parameters": parameters},
                "outputs": outputs,
                "steps": steps,
            }
        },
        "implementations": implementations,
    }


def main():
    database = Database(os.environ["V7_DATABASE_URL"])
    database.migrate()
    database.check_schema()
    admin = sa.create_engine(os.environ["V7_DATABASE_URL"], isolation_level="AUTOCOMMIT")
    services = []
    root = Path("installed-p4-runtime").resolve()
    try:
        for suffix, cls in (("data", DataDatabase), ("execution", ExecutionDatabase)):
            name = "v7_p4_" + suffix
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

        principal = database.create_principal("installed-p4-owner")
        token = database.issue_token(principal)["token"]
        root.mkdir()
        cluster = Cluster(root, database, services)
        cluster.env["V7_CWL_IMAGE"] = (P4_RUNTIME_ROOT / "cwl-image.txt").read_text().strip()
        cluster.env["V7_EXECUTION_CALLBACK_URL"] = cluster.workflow_callback_url
        try:
            cluster.start_services()
            cluster.bootstrap(token, budget=3)
            timeseries_targets = [
                value
                for value in cluster.targets
                if value["operator_id"] in {"timeseries-completeness", "statistics-linear-trend"}
            ]
            queued = cluster.request(
                "POST", cluster.p + "/workflow-packages", workflow(timeseries_targets)
            )
            workers = cluster.launch_platform_worker_lanes()

            def ready():
                status = workers.poll()
                assert status is None, (
                    f"Platform worker lane exited with status {status}; inspect {cluster.directory}"
                )
                value = cluster.request("GET", queued["status_url"])
                assert value["status"] != "REJECTED", value
                return value if value["status"] == "READY" else None

            package = eventually(ready, timeout=PACKAGE_READY_TIMEOUT)
            target = {"kind": "WORKFLOW", "package_ref": package["package_ref"]}
            cluster.request(
                "POST",
                cluster.p + "/grants",
                {
                    "task_id": cluster.task,
                    "targets": [target],
                    "resources": [cluster.selection["asset"]],
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
                    "target": target,
                    "inputs": {
                        "observations": cluster.selection,
                        "parameters": cluster.parameters,
                    },
                    "max_node_executions": 2,
                },
            )
            cluster.launch("execution-worker")
            execution = cluster.completed_execution(command, timeout=120)
            assert execution["state"] == "SUCCEEDED", execution
            steps = cluster.request(
                "GET",
                cluster.p + "/executions/" + execution["execution_id"] + "/steps",
            )["items"]
            assert len(steps) == 2 and all(item["state"] == "SUCCEEDED" for item in steps)
            path = cluster.p + "/tasks/" + cluster.task
            deliverable = cluster.request(
                "POST",
                path + "/deliverables",
                {
                    "expected_spec_revision": 1,
                    "role_bindings": cluster.exact_workflow_role_bindings(
                        execution, ("complete_result", "trend_result")
                    ),
                },
            )
            receipt = cluster.request(
                "POST",
                path + "/acceptance",
                {
                    "expected_spec_revision": 1,
                    "deliverable_id": deliverable["deliverable_id"],
                },
            )
            report = eventually(
                lambda: (
                    value
                    if (value := cluster.request("GET", receipt["status_url"]))["status"]
                    != "QUEUED"
                    else None
                ),
                timeout=60,
            )
            assert report["status"] == "PASS", json.dumps(report)
            assert cluster.request("GET", path)["lifecycle"] == "COMPLETED"
            assert cluster.client.get("/").status_code == 200
        finally:
            cluster.close()
    except BaseException:
        import sys

        for path in root.glob("process-*.log"):
            print(path.name + "\n" + path.read_text()[-6000:], file=sys.stderr)
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
