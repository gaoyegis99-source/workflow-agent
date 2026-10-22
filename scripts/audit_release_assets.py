#!/usr/bin/env python3
"""Read-only audit of every locked web asset and its source-map endpoint."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def fetch(origin: str, path: str) -> dict[str, object]:
    request = Request(origin.rstrip("/") + "/" + path.lstrip("/"), method="GET")
    try:
        with urlopen(request, timeout=10) as response:  # noqa: S310 - origin is explicit CLI input
            body = response.read()
            headers = {key.lower(): value for key, value in response.headers.items()}
            return {
                "status": response.status,
                "content_length": len(body),
                "sha256": "sha256:" + hashlib.sha256(body).hexdigest(),
                "content_type": headers.get("content-type"),
                "content_security_policy": headers.get("content-security-policy"),
            }
    except HTTPError as error:
        return {"status": error.code, "content_length": 0}
    except (OSError, URLError) as error:
        return {"status": None, "error": type(error).__name__}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default="http://127.0.0.1:18080")
    parser.add_argument(
        "--pointer",
        type=Path,
        default=Path("docs/verification/p82-current-release.json"),
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    pointer = json.loads(args.pointer.read_text(encoding="utf-8"))
    root = args.pointer.parent.parent.parent
    lock_path = root / pointer["release_directory"] / "release-lock.json"
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    assets = lock.get("web_assets", {})
    js_assets = sorted(path for path in assets if path.endswith(".js"))
    checks = []
    for asset in js_assets:
        response = fetch(args.origin, asset)
        response["path"] = "/" + asset
        response["expected_digest"] = assets[asset]
        response["digest_matches_lock"] = response.get("sha256") == assets[asset]
        map_response = fetch(args.origin, asset + ".map")
        checks.append(
            {
                "asset": response,
                "source_map": {"path": "/" + asset + ".map", **map_response},
            }
        )
    index = fetch(args.origin, "")
    result = {
        "schema_version": 1,
        "origin": args.origin,
        "release_version": pointer.get("version"),
        "release_digest": pointer.get("release_digest"),
        "locked_web_asset_count": len(assets),
        "locked_js_asset_count": len(js_assets),
        "asset_checks": checks,
        "source_maps": {
            "checked": len(checks),
            "unexpectedly_available": sum(
                item["source_map"].get("status") == 200 for item in checks
            ),
        },
        "root": {
            "status": index.get("status"),
            "content_security_policy": index.get("content_security_policy"),
            "csp_present": bool(index.get("content_security_policy")),
        },
        "production_business_data_read": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "js_assets": len(js_assets),
                "source_maps_200": result["source_maps"]["unexpectedly_available"],
            }
        )
    )
    return 0 if result["source_maps"]["unexpectedly_available"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
