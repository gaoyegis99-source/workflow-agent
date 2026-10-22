"""Subprocess harness for abrupt failures; absent from all distribution wheels."""

import argparse
import json
import logging
import os
import signal
import threading
from pathlib import Path

from peer_client import PeerClient
from platform_core.application.control import ControlService
from platform_infrastructure.database import Database
from platform_infrastructure.worker import Worker
from sqlalchemy import event


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["worker", "submit"])
    parser.add_argument("--request-file", type=Path)
    parser.add_argument("--marker", type=Path)
    parser.add_argument("--gate", choices=["before_commit", "after_commit"])
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    database = Database.from_environment()
    database.check_schema()
    try:
        if args.mode == "worker":
            stop = threading.Event()
            for signum in (signal.SIGTERM, signal.SIGINT):
                signal.signal(signum, lambda *_: stop.set())
            worker = Worker(
                database, PeerClient(os.environ["P2_PEER_URL"], os.environ["P2_PEER_TOKEN"])
            )
            worker.process_one() if args.once else worker.run(stop)
        else:
            actor = database.authenticate(os.environ["P2_ACTOR_TOKEN"])
            payload = json.loads(args.request_file.read_text())

            def gate(*_):
                args.marker.write_text("ready")
                threading.Event().wait(60)

            if args.gate == "before_commit":
                event.listen(database.engine, "commit", gate)
            result = ControlService(database).submit_action(
                actor, payload["project_id"], payload["key"], payload["request"]
            )
            if args.gate == "after_commit":
                gate()
            print(json.dumps(result))
    finally:
        database.close()


if __name__ == "__main__":
    main()
