"""Static migration contract for acceptance-lane leases.

The integration upgrade/downgrade test needs the project PostgreSQL container.
This test keeps the migration head and the metadata contract aligned even when
that controlled Docker environment is unavailable.
"""

import importlib

from platform_infrastructure import tables


def test_acceptance_lane_lease_metadata_and_migration_head_are_aligned():
    migration = importlib.import_module(
        "platform_infrastructure.migrations.versions.0026_worker_lane_leases"
    )

    assert (migration.revision, migration.down_revision) == (
        "0026_worker_lane_leases",
        "0025_exact_identity_only",
    )
    assert {"owner", "generation", "lease_until", "attempts"} <= {
        column.name for column in tables.acceptances.columns
    }
    assert "ix_acceptances_worker_ready" in {index.name for index in tables.acceptances.indexes}
