#!/usr/bin/env python3
"""Generate the exact source-and-image-locked P5-R2 capability catalogue."""

import argparse
import json
from pathlib import Path

from platform_core.application.checker_catalog import build_catalog


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--checker-image", required=True)
    parser.add_argument("--gdal-checker-image", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    images = {"CHECKER": args.checker_image, "GDAL_CHECKER": args.gdal_checker_image}
    value = {"schema_version": 1, "images": images, "catalog": build_catalog(images)}
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(value["catalog"]["catalog_digest"])


if __name__ == "__main__":
    main()
