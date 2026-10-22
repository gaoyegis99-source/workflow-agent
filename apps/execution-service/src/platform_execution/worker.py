"""Compose production adapters for the independent Execution application worker."""

import argparse
import logging
import os
import time

from platform_contracts.health import touch_worker_heartbeat
from sqlalchemy.exc import SQLAlchemyError

from platform_execution.adapters.docker import CwlDocker, Docker
from platform_execution.adapters.http import Client
from platform_execution.adapters.parquet import ParquetInspector
from platform_execution.adapters.repository import Repository
from platform_execution.application.runner import Runner
from platform_execution.application.workflow_runner import WorkflowRunner
from platform_execution.database import Database

ROLE_CLASSES = {
    "all": None,
    # Keep publication reconciliation as a secondary supervisor capability so
    # two workflows completing together cannot serialize on one fsync-heavy
    # publication worker. Node execution remains fully isolated.
    "supervisor": ("WORKFLOW_SUPERVISOR", "PUBLICATION_RECONCILE"),
    "node": ("NODE_EXECUTION",),
    "publication": ("PUBLICATION_RECONCILE",),
}


def Worker(
    database,
    root,
    docker,
    data,
    platform,
    workflow_docker=None,
    execution_url=None,
    role="all",
    credentials=None,
):
    if role not in ROLE_CLASSES:
        raise ValueError("Unknown Execution worker role")
    repository = Repository(database)
    workflow = (
        WorkflowRunner(repository, workflow_docker, data, platform, execution_url)
        if workflow_docker
        else None
    )
    if credentials is None:
        from platform_execution.adapters.remote import CredentialProvider

        credentials = CredentialProvider()
    from platform_execution.adapters.remote import RemoteClient

    return Runner(
        repository,
        root,
        docker,
        data,
        platform,
        workflow,
        work_classes=ROLE_CLASSES[role],
        remote_factory=lambda binding: RemoteClient(binding, credentials),
        table_inspector=ParquetInspector(),
    )


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=tuple(ROLE_CLASSES), default="all")
    args = parser.parse_args(argv)
    db = Database(os.environ["V7_EXECUTION_DATABASE_URL"])
    db.check_schema()
    touch_worker_heartbeat()
    data = Client(os.environ["V7_DATA_URL"], os.environ["V7_EXECUTION_DATA_TOKEN"])
    platform = Client(os.environ["V7_PLATFORM_URL"], os.environ["V7_EXECUTION_PLATFORM_TOKEN"])
    docker = Docker(
        os.environ["V7_OPERATOR_IMAGE"],
        os.environ["V7_CHECKER_IMAGE"],
        os.environ.get("V7_SPATIAL_QGIS_IMAGE_ID"),
        os.environ.get("V7_SPATIAL_NATIVE_IMAGE_ID"),
        os.environ.get("V7_GDAL_CHECKER_IMAGE"),
        os.environ.get("V7_TABULAR_OPERATOR_IMAGE"),
    )
    cwl = CwlDocker(os.environ["V7_CWL_IMAGE"]) if os.environ.get("V7_CWL_IMAGE") else None
    worker = Worker(
        db,
        os.environ["V7_EXECUTION_WORKDIR"],
        docker,
        data,
        platform,
        cwl,
        os.environ.get("V7_EXECUTION_CALLBACK_URL"),
        args.role,
    )
    try:
        while True:
            try:
                active = worker.tick()
            except (OSError, SQLAlchemyError):
                active = False
                logging.warning("Execution dependency unavailable")
            touch_worker_heartbeat()
            if not active:
                time.sleep(0.2)
    finally:
        data.close()
        platform.close()
        db.close()
