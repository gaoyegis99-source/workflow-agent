"""Compose production adapters for the independent Execution application worker."""

import argparse
import logging
import time

from platform_contracts.health import touch_worker_heartbeat
from sqlalchemy.exc import SQLAlchemyError

from platform_execution.adapters.docker import CwlDocker, Docker
from platform_execution.adapters.http import Client
from platform_execution.adapters.parquet import ParquetInspector
from platform_execution.adapters.repository import Repository
from platform_execution.application.runner import Runner
from platform_execution.application.workflow_runner import WorkflowRunner
from platform_execution.composition import ExecutionComposition
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
    runtime_config=None,
):
    if role not in ROLE_CLASSES:
        raise ValueError("Unknown Execution worker role")
    repository = Repository(database)
    workflow = (
        WorkflowRunner(
            repository,
            workflow_docker,
            data,
            platform,
            execution_url,
            runtime_config=runtime_config,
        )
        if workflow_docker
        else None
    )
    if credentials is None:
        from platform_execution.adapters.remote import CredentialProvider

        credentials = (
            CredentialProvider(runtime=runtime_config) if runtime_config else CredentialProvider()
        )
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
    composition = ExecutionComposition.from_environment()
    db = Database(composition.database_url)
    db.check_schema()
    touch_worker_heartbeat(composition.runtime.worker_heartbeat_file)
    data = Client(
        composition.data.url,
        composition.data.token,
        deployment=composition.runtime.deployment,
    )
    platform = Client(
        composition.platform.url,
        composition.platform.token,
        deployment=composition.runtime.deployment,
    )
    docker = Docker(
        composition.catalog.image("operator"),
        composition.catalog.image("checker"),
        composition.catalog.image("qgis"),
        composition.catalog.image("spatial_native"),
        composition.catalog.image("gdal_checker"),
        composition.catalog.image("tabular"),
        container_identity=composition.runtime.container_identity,
    )
    cwl = (
        CwlDocker(
            composition.workflow_image,
            container_identity=composition.runtime.container_identity,
            workload_network=composition.runtime.workload_network,
        )
        if composition.workflow_image
        else None
    )
    worker = Worker(
        db,
        composition.runtime.work_root,
        docker,
        data,
        platform,
        cwl,
        composition.runtime.workflow_engine_callback_url,
        args.role,
        runtime_config=composition.runtime,
    )
    try:
        while True:
            try:
                active = worker.tick()
            except (OSError, SQLAlchemyError):
                active = False
                logging.warning("Execution dependency unavailable")
            touch_worker_heartbeat(composition.runtime.worker_heartbeat_file)
            if not active:
                time.sleep(0.2)
    finally:
        data.close()
        platform.close()
        db.close()
