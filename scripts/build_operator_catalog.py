#!/usr/bin/env python3
"""Generate or verify the deployment-frozen P7 Operator Catalog."""

import argparse
import json
import os

from platform_core.application.catalog import load_catalog, write_catalog


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("generate", "check"))
    parser.add_argument("--output", default=os.environ.get("V7_OPERATOR_CATALOG"))
    args = parser.parse_args()
    if not args.output:
        raise SystemExit("--output or V7_OPERATOR_CATALOG is required")
    if args.command == "generate":
        required = (
            "V7_OPERATOR_IMAGE",
            "V7_SPATIAL_QGIS_IMAGE_ID",
            "V7_SPATIAL_NATIVE_IMAGE_ID",
            "V7_TABULAR_OPERATOR_IMAGE",
        )
        invalid = [
            name
            for name in required
            if not (
                isinstance(os.environ.get(name), str)
                and len(os.environ[name]) == 71
                and os.environ[name].startswith("sha256:")
            )
        ]
        if invalid:
            raise SystemExit("immutable image IDs are required: " + ", ".join(invalid))
    value = write_catalog(args.output) if args.command == "generate" else load_catalog(args.output)
    print(
        json.dumps(
            {"catalog_digest": value["catalog_digest"], "operators": len(value["operators"])}
        )
    )


if __name__ == "__main__":
    main()
