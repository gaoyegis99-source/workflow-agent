from scripts.maintenance.cleanup_core import CleanupCore
from scripts.maintenance.cleanup_protocol import (
    CLEANUP_PROTOCOL_VERSION,
    LIFECYCLE,
    REQUIRED_FILE_GUARDS,
)


def test_cleanup_core_exposes_the_local_protocol_revision():
    assert CleanupCore.protocol_version == CLEANUP_PROTOCOL_VERSION
    assert LIFECYCLE == ("plan", "stage", "verify", "rebuild", "purge")


def test_cleanup_protocol_keeps_the_non_negotiable_file_guards():
    assert REQUIRED_FILE_GUARDS == {
        "SYMLINK",
        "HARDLINK",
        "PATH_ESCAPE",
        "REFERENCE",
        "PROTECTED",
        "REBUILD_PROOF",
    }
