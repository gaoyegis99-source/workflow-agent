"""Refuse invocation of the retired Release-Lock upgrade console command."""

from __future__ import annotations


def main() -> None:
    raise SystemExit(
        "RETIRED_RELEASE_LOCK_UPGRADE: use direct Compose maintenance "
        "(bin/configure-p8, bin/p8 preflight, cold backup, bin/p8 up)"
    )
