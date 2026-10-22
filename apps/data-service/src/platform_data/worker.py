"""Independent import worker, with lease fencing and immutable normalized bytes."""

import logging
import os
import threading
import time
from uuid import uuid4

import pyarrow as pa
from platform_contracts.health import touch_worker_heartbeat
from platform_contracts.service import ServiceFault
from sqlalchemy.exc import SQLAlchemyError

from platform_data.adapters.blobs import Blobs
from platform_data.adapters.repository import Repository
from platform_data.adapters.tables_io import normalize
from platform_data.database import Database


class Worker:
    def __init__(self, database, blobs):
        self.repo, self.blobs, self.owner = Repository(database), blobs, uuid4().hex
        self.lease_seconds, self.heartbeat_seconds = 30, 10

    def tick(self):
        claim = self.repo.claim(self.owner, self.lease_seconds, formats=("csv", "parquet"))
        if not claim:
            return False
        stop = threading.Event()

        def heartbeat():
            while not stop.wait(self.heartbeat_seconds):
                try:
                    if not self.repo.renew(claim, self.lease_seconds):
                        return
                except SQLAlchemyError:
                    logging.warning(
                        "Data lease renewal unavailable upload_id=%s generation=%s",
                        claim["upload_id"],
                        claim["generation"],
                    )
                    return

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        payload = claim["payload"]
        try:
            raw = self.blobs.read(payload["digest"])
            normalized, fields, count = normalize(raw, payload["format"], payload["conversion"])
            checksum = self.blobs.store(normalized)
            manifest = {
                "asset_id": claim["upload_id"],
                "version_id": "1",
                "project_id": claim["project_id"],
                "kind": "OBSERVATIONS",
                "fields": fields,
                "row_count": count,
                "source": {"upload_id": claim["upload_id"]},
                "representations": [
                    {
                        "representation_id": "original",
                        "format": payload["format"],
                        "digest": payload["digest"],
                        "size": len(raw),
                    },
                    {
                        "representation_id": "normalized",
                        "format": "parquet",
                        "digest": checksum,
                        "size": len(normalized),
                    },
                ],
            }
            self.repo.import_result(claim, manifest)
        except (ServiceFault, pa.ArrowException, ValueError, UnicodeError) as exc:
            # No raw input values or host paths are recorded in errors.
            self.repo.import_result(
                claim,
                error={
                    "code": "MALFORMED_REQUEST",
                    "message": "Import rejected: invalid or unsupported table",
                    "category": type(exc).__name__,
                },
            )
        finally:
            stop.set()
            thread.join(timeout=self.heartbeat_seconds + 4)
        return True


def main():
    database = Database(os.environ["V7_DATA_DATABASE_URL"])
    database.check_schema()
    touch_worker_heartbeat()
    worker = Worker(database, Blobs(os.environ["V7_DATA_STORAGE"]))
    try:
        while True:
            try:
                active = worker.tick()
            except (SQLAlchemyError, OSError) as exc:
                logging.warning("Data worker dependency unavailable: %s", type(exc).__name__)
                active = False
            touch_worker_heartbeat()
            if not active:
                time.sleep(0.5)
    finally:
        database.close()
