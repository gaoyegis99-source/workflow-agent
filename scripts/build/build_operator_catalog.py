#!/usr/bin/env python3
"""Generate or verify the deployment-frozen P7 Operator Catalog."""

import argparse
import json
import os

from platform_core.application.catalog import load_catalog, write_catalog

IMAGE_ENVIRONMENT = {
    "OPERATOR": "V7_OPERATOR_IMAGE",
    "QGIS": "V7_SPATIAL_QGIS_IMAGE_ID",
    "SPATIAL_NATIVE": "V7_SPATIAL_NATIVE_IMAGE_ID",
    "TABULAR": "V7_TABULAR_OPERATOR_IMAGE",
}


def deployment_images(environment):
    images = {role: environment.get(name) for role, name in IMAGE_ENVIRONMENT.items()}
    invalid = [
        IMAGE_ENVIRONMENT[role]
        for role, value in images.items()
        if not (
            isinstance(value, str)
            and len(value) == 71
            and value.startswith("sha256:")
            and all(character in "0123456789abcdef" for character in value[7:])
        )
    ]
    if invalid:
        raise ValueError("immutable image IDs are required: " + ", ".join(invalid))
    return images


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("generate", "check"))
    parser.add_argument("--output", default=os.environ.get("V7_OPERATOR_CATALOG"))
    args = parser.parse_args()
    if not args.output:
        raise SystemExit("--output or V7_OPERATOR_CATALOG is required")
    try:
        images = deployment_images(os.environ) if args.command == "generate" else None
    except ValueError as error:
        raise SystemExit(str(error)) from error
    value = (
        write_catalog(args.output, images)
        if args.command == "generate"
        else load_catalog(args.output)
    )
    print(
        json.dumps(
            {"catalog_digest": value["catalog_digest"], "operators": len(value["operators"])}
        )
    )


if __name__ == "__main__":
    main()
