#!/usr/bin/env python3
"""Generate a digest-frozen model catalogue without provider credentials."""

import argparse
import json
from pathlib import Path

from platform_core.application.model_catalog import build_catalog
from platform_core.domain.agent_runtime import canonical_digest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profiles", required=True, help="JSON array of model profiles")
    parser.add_argument("--default-profile", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    source = json.loads(Path(args.profiles).read_text(encoding="utf-8"))
    if not isinstance(source, list):
        raise ValueError("profiles input must be a JSON array")
    profiles = []
    for item in source:
        if not isinstance(item, dict):
            raise ValueError("each model profile must be an object")
        unsigned = {key: value for key, value in item.items() if key != "profile_digest"}
        profiles.append(unsigned | {"profile_digest": canonical_digest(unsigned)})
    catalog = build_catalog(profiles, args.default_profile)
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(catalog["catalog_digest"])


if __name__ == "__main__":
    main()
