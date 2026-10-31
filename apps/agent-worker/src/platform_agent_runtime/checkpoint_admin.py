"""Explicit P6 Agent checkpoint migration CLI."""

import argparse
import json

from platform_agent_runtime.checkpoint import SCHEMA_VERSION, CheckpointDatabase
from platform_agent_runtime.composition import AgentCheckpointComposition


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["migrate", "check-schema"])
    args = parser.parse_args()
    composition = AgentCheckpointComposition.from_environment()
    database = CheckpointDatabase(composition.checkpoint_database_url)
    if args.command == "migrate":
        database.migrate()
    else:
        database.check_schema()
    print(json.dumps({"status": "ok", "schema_version": SCHEMA_VERSION}))
