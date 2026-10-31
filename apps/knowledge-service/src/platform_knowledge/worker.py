"""Recoverable Knowledge import worker using exact Data references."""

import hashlib
import logging
import threading
import time
from uuid import uuid4

from platform_contracts.health import touch_worker_heartbeat
from platform_contracts.service import ServiceFault, ensure
from sqlalchemy.exc import SQLAlchemyError

from platform_knowledge.blobs import Blobs
from platform_knowledge.composition import KnowledgeComposition
from platform_knowledge.data_client import DataClient
from platform_knowledge.database import Database
from platform_knowledge.domain import normalize, segment
from platform_knowledge.repository import Repository


class Worker:
    def __init__(self, database, blobs, data):
        self.repo, self.blobs, self.data = Repository(database), blobs, data
        self.owner = uuid4().hex
        self.lease_seconds, self.heartbeat_seconds = 30, 10

    def tick(self):
        claim = self.repo.claim(self.owner, self.lease_seconds)
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
                        "Knowledge lease renewal unavailable import_id=%s generation=%s",
                        claim["import_id"],
                        claim["generation"],
                    )
                    return

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        processed = True
        try:
            payload = claim["payload"]
            project, source = claim["project_id"], payload["source"]
            ensure(source["project_id"] == project, "SCOPE_DENIED", "Source project differs", 403)
            asset = self.data.asset(project, source)
            ensure(asset["ref"] == source, "INVALID_BINDING", "Source asset changed")
            manifest = asset["manifest"]
            ensure(
                manifest.get("kind") == "DOCUMENT", "INVALID_BINDING", "Source is not a document"
            )
            representation = next(
                (
                    item
                    for item in manifest.get("representations", [])
                    if item["representation_id"] == payload["representation_id"]
                ),
                None,
            )
            ensure(
                representation is not None and representation["format"] in {"markdown", "text"},
                "INVALID_BINDING",
                "Unsupported document representation",
            )
            raw, response_digest = self.data.content(project, source, payload["representation_id"])
            ensure(
                response_digest == representation["digest"]
                and "sha256:" + hashlib.sha256(raw).hexdigest() == representation["digest"],
                "INVALID_BINDING",
                "Source representation digest mismatch",
            )
            text = normalize(raw)
            content = text.encode()
            content_digest = self.blobs.store(content)
            document_id, version_id = self.repo.identities(project, source)
            units = segment(text)
            for unit in units:
                unit["unit_id"] = hashlib.sha256(
                    f"{document_id}:{version_id}:{unit['start_char']}:{unit['end_char']}:{unit['exact_text_digest']}".encode()
                ).hexdigest()[:32]
            self.repo.import_result(
                claim,
                {
                    "document_id": document_id,
                    "version_id": version_id,
                    "title": manifest["title"],
                    "source": source,
                    "source_digest": representation["digest"],
                    "content_digest": content_digest,
                    "format": representation["format"],
                    "units": units,
                },
            )
        except ServiceFault as exc:
            if exc.code == "DEPENDENCY_UNAVAILABLE":
                self.repo.retry(claim)
                processed = False
            else:
                self.repo.import_result(
                    claim,
                    error={
                        "code": exc.code,
                        "message": "Knowledge import failed",
                        "category": type(exc).__name__,
                    },
                )
        except OSError:
            self.repo.retry(claim)
            processed = False
        except (UnicodeError, ValueError) as exc:
            self.repo.import_result(
                claim,
                error={
                    "code": "MALFORMED_REQUEST",
                    "message": "Knowledge import failed",
                    "category": type(exc).__name__,
                },
            )
        finally:
            stop.set()
            thread.join(timeout=self.heartbeat_seconds + 4)
        return processed


def main():
    composition = KnowledgeComposition.from_environment()
    database = Database(composition.database_url)
    database.check_schema()
    touch_worker_heartbeat(composition.heartbeat_file)
    data = DataClient(
        composition.data.url,
        composition.data.token,
        deployment=composition.deployment,
    )
    worker = Worker(database, Blobs(composition.storage_root), data)
    try:
        while True:
            try:
                active = worker.tick()
            except (SQLAlchemyError, OSError) as exc:
                logging.warning("Knowledge worker dependency unavailable: %s", type(exc).__name__)
                active = False
            touch_worker_heartbeat(composition.heartbeat_file)
            if not active:
                time.sleep(0.5)
    finally:
        data.close()
        database.close()
