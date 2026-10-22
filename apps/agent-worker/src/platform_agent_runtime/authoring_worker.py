"""Dedicated Capability Studio validation worker composition root."""

import argparse
import logging
import os
import signal
import threading
from importlib.metadata import version
from uuid import uuid4

from platform_contracts.authoring import (
    build_authoring_descriptor,
    validate_operator_parameters,
)
from platform_contracts.health import touch_worker_heartbeat
from platform_contracts.release import production_mode, production_release
from platform_core.application.authoring import (
    AuthoringValidationProcessor,
    semantic_environment,
)
from platform_core.application.authoring_release import TrialVerdictProcessor
from platform_core.application.catalog import configured_catalog
from platform_infrastructure.cwl import CwlValidator
from platform_infrastructure.database import Database, StorageError
from platform_infrastructure.services import from_environment

logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="V7 trusted workflow authoring validator")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    database = None
    try:
        image_id = os.environ.get("V7_CWL_IMAGE")
        if not image_id:
            raise RuntimeError("V7_CWL_IMAGE is required")
        database = Database.from_environment()
        database.check_schema()
        touch_worker_heartbeat()
        catalog = configured_catalog()
        release = None
        if production_mode():
            operator_path = os.environ.get("V7_OPERATOR_CATALOG")
            if not operator_path:
                raise RuntimeError("Production authoring worker requires the Operator Catalog")
            release = production_release(
                operator_path=operator_path,
                operator_catalog=catalog,
                images={"cwl": image_id},
                version=version("groundwater-agent-runtime"),
            )
        descriptor = build_authoring_descriptor(catalog)
        environment = semantic_environment(
            descriptor,
            image_id,
            release["release_digest"] if release else None,
        )
        processor = AuthoringValidationProcessor(
            database,
            CwlValidator(image_id),
            descriptor,
            validate_operator_parameters,
            environment,
            owner=uuid4().hex,
        )
        data, execution = from_environment()
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
                touch_worker_heartbeat()
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
