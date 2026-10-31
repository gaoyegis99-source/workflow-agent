#!/usr/bin/env python3
"""Retired Release-Lock builder.

Direct-Compose deployments take an explicit artifact directory and immutable
image identifiers through ``bin/configure-p8``. This retained filename fails
closed so old automation cannot silently create a new lock-era release.
"""

from __future__ import annotations

import sys


def main() -> None:
    print(
        "RETIRED_RELEASE_LOCK_BUILD: use bin/configure-p8 with an artifact directory "
        "and explicit immutable image inputs",
        file=sys.stderr,
    )
    raise SystemExit(64)


if __name__ == "__main__":
    main()
