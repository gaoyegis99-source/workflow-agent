"""Versioned, project-local contract for the cleanup core.

The two groundwater projects deliberately keep independent cleanup code and
policies.  This module names the shared *behavioural* contract without making
either project import the other one at runtime.
"""

from __future__ import annotations

CLEANUP_PROTOCOL_VERSION = "cleanup-protocol.v1"
LIFECYCLE = ("plan", "stage", "verify", "rebuild", "purge")
REQUIRED_FILE_GUARDS = frozenset(
    {
        "SYMLINK",
        "HARDLINK",
        "PATH_ESCAPE",
        "REFERENCE",
        "PROTECTED",
        "REBUILD_PROOF",
    }
)
