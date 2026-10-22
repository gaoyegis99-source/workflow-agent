"""Small installation probe for P6.0 runtime profiles."""

import argparse
import json

from platform_agent_runtime.profiles import (
    LANGGRAPH_PROFILE_ID,
    PLAIN_PROFILE_ID,
    diagnostic,
    profile,
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=[PLAIN_PROFILE_ID, LANGGRAPH_PROFILE_ID])
    args = parser.parse_args()
    profiles = [args.profile] if args.profile else [PLAIN_PROFILE_ID, LANGGRAPH_PROFILE_ID]
    print(json.dumps([diagnostic(profile(item)) for item in profiles], indent=2, sort_keys=True))
