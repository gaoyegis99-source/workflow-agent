"""Test-only process plugin implementing the v7-process-plugin-v1 protocol."""

from __future__ import annotations

import json
import os
import sys


def main() -> int:
    request = json.load(sys.stdin)
    action = request["action"]
    if request["operation"] == "reconcile" and action["inputs"].get("known") is not True:
        print("null")
        return 0
    if action["inputs"].get("malformed") is True:
        print("not-json")
        return 0
    print(
        json.dumps(
            {
                "action_id": action["action_id"],
                "status": "SUCCEEDED",
                "output": {
                    "echo": action["inputs"],
                    "parent_environment_was_present": bool(os.environ.get("V7_TEST_PARENT_SECRET")),
                    "protocol": os.environ.get("V7_PLUGIN_PROTOCOL"),
                },
                "error_code": None,
                "external_reference": None,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
