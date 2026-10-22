"""Copied outside the repository for an installed-wheel, real-service business smoke test."""

import os
import secrets
from pathlib import Path

import sqlalchemy as sa
from platform_data.database import Database as DataDatabase
from platform_execution.database import Database as ExecutionDatabase
from platform_infrastructure.database import Database

from tests.p2.processes import eventually
from tests.p3.harness import Cluster


def main():
    db = Database.from_environment()
    db.migrate()
    db.check_schema()
    admin = sa.create_engine(os.environ["V7_DATABASE_URL"], isolation_level="AUTOCOMMIT")
    services = []
    try:
        for name, cls in [("v7_data", DataDatabase), ("v7_execution", ExecutionDatabase)]:
            password = secrets.token_hex(24)
            with admin.connect() as c:
                c.exec_driver_sql(f"CREATE ROLE {name} LOGIN PASSWORD '{password}'")
                c.exec_driver_sql(f"CREATE DATABASE {name} OWNER {name}")
                c.exec_driver_sql(f"REVOKE ALL ON DATABASE {name} FROM PUBLIC")
            url = admin.url.set(username=name, password=password, database=name)
            service = cls(url.render_as_string(hide_password=False))
            service.migrate()
            service.migrate()
            service.check_schema()
            services.append(service)
        principal = db.create_principal("installed-owner")
        token = db.issue_token(principal)["token"]
        root = Path("installed-runtime").resolve()
        root.mkdir()
        cluster = Cluster(root, db, services)
        try:
            cluster.start_services()
            cluster.bootstrap(token)
            commands = [cluster.submit(index=i) for i in range(2)]
            cluster.launch("platform-worker")
            cluster.launch("execution-worker")
            executions = [cluster.completed_execution(command) for command in commands]
            assert all(value["state"] == "SUCCEEDED" for value in executions)
            path = cluster.p + "/tasks/" + cluster.task
            eventually(
                lambda: cluster.request("GET", path + "/budget")["in_flight"] == 0, timeout=45
            )
            candidate = cluster.request(
                "POST",
                path + "/deliverables",
                {
                    "expected_spec_revision": 1,
                    "executions": [v["execution_id"] for v in executions],
                },
            )
            receipt = cluster.request(
                "POST",
                path + "/acceptance",
                {"expected_spec_revision": 1, "deliverable_id": candidate["deliverable_id"]},
            )
            eventually(lambda: cluster.request("GET", path)["lifecycle"] == "COMPLETED", timeout=60)
            assert cluster.request("GET", receipt["status_url"])["status"] == "PASS"
            assert cluster.request("GET", path + "/budget")["used"] == 3
            assert cluster.client.get("/").status_code == 200
        except BaseException:
            import sys

            for path in root.glob("process-*.log"):
                print(path.name + "\n" + path.read_text()[-6000:], file=sys.stderr)
            raise
        finally:
            cluster.close()
    finally:
        for service in services:
            service.close()
        db.close()
        admin.dispose()


if __name__ == "__main__":
    main()
