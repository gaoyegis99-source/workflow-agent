"""Dedicated UTF-8 document import worker; it never parses or indexes document meaning."""

import logging
import os
import threading
import time
from uuid import uuid4

from platform_contracts.health import touch_worker_heartbeat
from platform_contracts.service import ServiceFault, ensure
from sqlalchemy.exc import SQLAlchemyError

from platform_data.adapters.blobs import Blobs
from platform_data.adapters.repository import Repository
from platform_data.database import Database


def inspect_document(raw: bytes, format_id: str) -> dict:
    ensure(format_id in {"markdown", "text"}, "MALFORMED_REQUEST", "Unsupported document")
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    ensure(text.strip() != "", "MALFORMED_REQUEST", "Document has no textual content", 422)
    ensure("\x00" not in text, "MALFORMED_REQUEST", "Document contains binary data", 422)
    ensure(
        all(character in "\n\r\t\f" or ord(character) >= 32 for character in text),
        "MALFORMED_REQUEST",
        "Document contains unsupported control characters",
        422,
    )
    return {"encoding": "utf-8", "utf8_bom": bom, "character_count": len(text)}


class DocumentWorker:
    def __init__(self, database, blobs):
        self.repo, self.blobs, self.owner = Repository(database), blobs, uuid4().hex
        self.lease_seconds, self.heartbeat_seconds = 30, 10

    def tick(self):
        claim = self.repo.claim(self.owner, self.lease_seconds, formats=("markdown", "text"))
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
                        "Document lease renewal unavailable upload_id=%s generation=%s",
                        claim["upload_id"],
                        claim["generation"],
                    )
                    return

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        payload = claim["payload"]
        try:
            raw = self.blobs.read(payload["digest"])
            facts = inspect_document(raw, payload["format"])
            manifest = {
                "asset_id": claim["upload_id"],
                "version_id": "1",
                "project_id": claim["project_id"],
                "kind": "DOCUMENT",
                "title": payload["filename"],
                "document": facts,
                "source": {"upload_id": claim["upload_id"]},
                "representations": [
                    {
                        "representation_id": "original",
                        "format": payload["format"],
                        "digest": payload["digest"],
                        "size": len(raw),
                    }
                ],
            }
            self.repo.import_result(claim, manifest)
        except (ServiceFault, UnicodeError, ValueError) as exc:
            self.repo.import_result(
                claim,
                error={
                    "code": "MALFORMED_REQUEST",
                    "message": "Import rejected: invalid UTF-8 document",
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
    worker = DocumentWorker(database, Blobs(os.environ["V7_DATA_STORAGE"]))
    try:
        while True:
            try:
                active = worker.tick()
            except (SQLAlchemyError, OSError) as exc:
                logging.warning("Document worker dependency unavailable: %s", type(exc).__name__)
                active = False
            touch_worker_heartbeat()
            if not active:
                time.sleep(0.5)
    finally:
        database.close()
