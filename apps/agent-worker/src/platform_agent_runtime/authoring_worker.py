"""Dedicated Capability Studio validation worker composition root."""

import argparse
import logging
import signal
import threading
from uuid import uuid4

from platform_contracts.authoring import (
    build_authoring_descriptor,
    validate_operator_parameters,
)
from platform_contracts.health import touch_worker_heartbeat
from platform_core.application.authoring import (
    AuthoringValidationProcessor,
    semantic_environment,
)
from platform_core.application.trial_verdict import TrialVerdictProcessor
from platform_infrastructure.cwl import CwlValidator
from platform_infrastructure.database import Database, StorageError
from platform_infrastructure.services import data_client, execution_client

from platform_agent_runtime.composition import AuthoringComposition

logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="V7 trusted workflow authoring validator")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    database = None
    try:
        composition = AuthoringComposition.from_environment()
        image_id = composition.cwl_image
        database = Database(composition.database_url)
        database.check_schema()
        touch_worker_heartbeat(composition.heartbeat_file)
        catalog = composition.operator_catalog
        descriptor = build_authoring_descriptor(catalog)
        environment = semantic_environment(
            descriptor,
            image_id,
            composition.deployment.deployment_revision,
        )
        processor = AuthoringValidationProcessor(
            database,
            CwlValidator(image_id),
            descriptor,
            validate_operator_parameters,
            environment,
            operator_catalog=catalog,
            owner=uuid4().hex,
        )
        data = data_client(composition.data, deployment=composition.deployment)
        execution = execution_client(composition.execution, deployment=composition.deployment)
        trial_processor = (
            TrialVerdictProcessor(
                database,
                data,
                execution,
                owner=uuid4().hex,
            )
            if data is not None and execution is not None
            else None
        )
        if args.once:
            if not processor.process_one() and trial_processor is not None:
                trial_processor.process_one()
            return
        stop = threading.Event()
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: stop.set())
        validation_turn = True
        while not stop.is_set():
            try:
                if validation_turn or trial_processor is None:
                    worked = processor.process_one()
                    if not worked and trial_processor is not None:
                        worked = trial_processor.process_one()
                else:
                    worked = trial_processor.process_one()
                    if not worked:
                        worked = processor.process_one()
                validation_turn = not validation_turn
                touch_worker_heartbeat(composition.heartbeat_file)
                if not worked:
                    stop.wait(0.2)
            except StorageError:
                logger.warning("Authoring validation database unavailable; retrying")
                stop.wait(1)
    except (StorageError, RuntimeError, ValueError):
        logger.error("Authoring validation worker startup failed")
        raise SystemExit(1) from None
    finally:
        if "data" in locals() and data is not None:
            data.close()
        if "execution" in locals() and execution is not None:
            execution.close()
        if database:
            database.close()


if __name__ == "__main__":
    main()
