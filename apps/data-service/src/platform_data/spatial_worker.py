"""Recoverable spatial import worker; QGIS/GDAL live only in a fixed container."""

import json
import logging
import subprocess
import threading
import time
from pathlib import Path
from uuid import uuid4

from platform_contracts.health import touch_worker_heartbeat
from sqlalchemy.exc import SQLAlchemyError

from platform_data.adapters.blobs import Blobs
from platform_data.adapters.repository import Repository
from platform_data.adapters.spatial import SpatialInspector
from platform_data.composition import DataComposition
from platform_data.database import Database


class SpatialWorker:
    def __init__(self, database, blobs, inspector, *, heartbeat_file: Path | None = None):
        self.repo, self.blobs, self.inspector = Repository(database), blobs, inspector
        self.owner = uuid4().hex
        self.lease_seconds, self.heartbeat_seconds = 30, 10
        self.heartbeat_file = heartbeat_file

    def _error(self, claim, reason, category="SpatialInputError"):
        return self.repo.import_result(
            claim,
            error={
                "code": "MALFORMED_REQUEST",
                "message": "Import rejected: invalid or unsupported spatial asset",
                "category": category,
                "reason": reason,
            },
        )

    def tick(self):
        claim = self.repo.claim(self.owner, self.lease_seconds, formats=("geopackage", "geotiff"))
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
                        "Spatial import lease renewal unavailable upload_id=%s generation=%s",
                        claim["upload_id"],
                        claim["generation"],
                    )
                    return

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        info = None
        settled = False
        try:
            runtime = claim.get("runtime") or {}
            info = self.inspector.inspect(claim["upload_id"])
            if info is None:
                if runtime.get("phase") == "STARTED":
                    settled = self._error(claim, "INSPECTION_IDENTITY_LOST", "RecoveryError")
                    return True
                self.repo.update_import_runtime(
                    claim,
                    phase="CREATE_INTENT",
                    image_id=self.inspector.image_id,
                    source_digest=claim["payload"]["digest"],
                )
                info = self.inspector.create(claim, self.blobs.path(claim["payload"]["digest"]))
            self.inspector.check(claim, info)
            started = not info["State"]["StartedAt"].startswith("0001-")
            if not started:
                self.inspector.start(info["Id"])
            self.repo.update_import_runtime(
                claim, phase="STARTED", container_id=info["Id"], image_id=self.inspector.image_id
            )
            deadline = time.monotonic() + 180
            while True:
                info = self.inspector.inspect(claim["upload_id"])
                if info is None:
                    settled = self._error(claim, "INSPECTION_IDENTITY_LOST", "RecoveryError")
                    return True
                self.inspector.check(claim, info)
                if not info["State"]["Running"]:
                    break
                touch_worker_heartbeat(self.heartbeat_file)
                if time.monotonic() >= deadline:
                    self.inspector.stop(info["Id"])
                    settled = self._error(claim, "SPATIAL_INSPECTION_TIMEOUT", "ResourceLimit")
                    return True
                time.sleep(0.2)
            _, output = self.inspector.paths(claim)
            if info["State"]["ExitCode"] != 0:
                reason, category = "SPATIAL_INPUT_INVALID", "SpatialInputError"
                error_file = output / "error.json"
                if error_file.is_file() and error_file.stat().st_size < 4096:
                    value = json.loads(error_file.read_text())
                    reason = str(value.get("reason", reason))[:128]
                    category = str(value.get("category", category))[:128]
                settled = self._error(claim, reason, category)
                return True
            descriptor = json.loads((output / "inspection.json").read_text())
            if descriptor.get("source") != {
                "digest": claim["payload"]["digest"],
                "size": claim["payload"]["size"],
            }:
                raise ValueError("Spatial inspection source identity changed")
            manifest = {
                "asset_id": claim["upload_id"],
                "version_id": "1",
                "project_id": claim["project_id"],
                "kind": descriptor["kind"],
                "driver": descriptor["driver"],
                "spatial": descriptor["spatial"],
                "source": {
                    "upload_id": claim["upload_id"],
                    "filename": claim["payload"]["filename"],
                    "digest": claim["payload"]["digest"],
                    "size": claim["payload"]["size"],
                },
                "representations": [
                    {
                        "representation_id": "original",
                        "format": claim["payload"]["format"],
                        "digest": claim["payload"]["digest"],
                        "size": claim["payload"]["size"],
                    }
                ],
            }
            self.repo.update_import_runtime(claim, phase="COLLECTED")
            settled = self.repo.import_result(claim, manifest)
        except (OSError, subprocess.TimeoutExpired, SQLAlchemyError) as exc:
            # Docker may continue a create/start request after the bounded CLI
            # call loses its response.  Keep CREATE_INTENT/STARTED durable and
            # leave the deterministic container in place; the expired lease
            # lets the next tick inspect and reconcile the same external effect.
            logging.warning(
                "Spatial import dependency unavailable upload_id=%s generation=%s "
                "phase=%s category=%s",
                claim["upload_id"],
                claim["generation"],
                (claim.get("runtime") or {}).get("phase", "CLAIMED"),
                type(exc).__name__,
            )
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            logging.warning(
                "Spatial import failed upload_id=%s category=%s",
                claim["upload_id"],
                type(exc).__name__,
            )
            settled = self._error(claim, "SPATIAL_INSPECTION_FAILED", type(exc).__name__)
        finally:
            stop.set()
            thread.join(timeout=self.heartbeat_seconds + 4)
            if settled and info is not None and not info.get("State", {}).get("Running", False):
                self.inspector.remove(info["Id"])
        return True


def main():
    composition = DataComposition.from_environment(role="spatial")
    if composition.spatial_image is None or composition.spatial_work_root is None:
        raise RuntimeError("V7_SPATIAL_COMPOSITION_INVALID")
    database = Database(composition.database_url)
    database.check_schema()
    touch_worker_heartbeat(composition.heartbeat_file)
    worker = SpatialWorker(
        database,
        Blobs(composition.storage_root),
        SpatialInspector(
            composition.spatial_image,
            composition.spatial_work_root,
            container_identity=composition.container_identity,
        ),
        heartbeat_file=composition.heartbeat_file,
    )
    try:
        while True:
            try:
                active = worker.tick()
            except (SQLAlchemyError, OSError) as exc:
                logging.warning("Spatial worker dependency unavailable: %s", type(exc).__name__)
                active = False
            touch_worker_heartbeat(composition.heartbeat_file)
            if not active:
                time.sleep(0.5)
    finally:
        database.close()


if __name__ == "__main__":
    main()
