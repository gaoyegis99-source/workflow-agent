#!/usr/bin/env python3
"""Export the selected allowlisted P6 runtime profile for a deployment."""

import argparse
import json
from pathlib import Path

from platform_agent_runtime.profiles import profile
from platform_core.application.embedding_catalog import load_embedding_catalog


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--embedding-catalog", required=True)
    args = parser.parse_args()
    embedding = load_embedding_catalog(args.embedding_catalog)
    value = profile() | {"embedding_catalog_digest": embedding["catalog_digest"]}
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(value["profile_digest"])


if __name__ == "__main__":
    main()
