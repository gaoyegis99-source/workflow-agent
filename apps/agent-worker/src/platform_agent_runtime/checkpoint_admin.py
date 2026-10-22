"""Explicit P6 Agent checkpoint migration CLI."""

import argparse
import json
import os

from platform_agent_runtime.checkpoint import SCHEMA_VERSION, CheckpointDatabase


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["migrate", "check-schema"])
    args = parser.parse_args()
    url = os.environ.get("V7_AGENT_CHECKPOINT_DATABASE_URL")
    if not url:
        raise SystemExit("V7_AGENT_CHECKPOINT_DATABASE_URL is required")
    database = CheckpointDatabase(url)
    if args.command == "migrate":
        database.migrate()
    else:
        database.check_schema()
    print(json.dumps({"status": "ok", "schema_version": SCHEMA_VERSION}))
