"""Test-owned Execution entrypoint selecting one trusted profile by dependency injection."""

import argparse
import os

import uvicorn
from platform_contracts.engine_runtime import OFF_PROFILE_ID, ON_PROFILE_ID
from platform_execution.api import create_app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=(OFF_PROFILE_ID, ON_PROFILE_ID), required=True)
    args = parser.parse_args()
    uvicorn.run(
        create_app(workflow_profile_id=args.profile),
        host=os.environ.get("V7_EXECUTION_HOST", "127.0.0.1"),
        port=int(os.environ["V7_EXECUTION_PORT"]),
        access_log=False,
    )


if __name__ == "__main__":
    main()
