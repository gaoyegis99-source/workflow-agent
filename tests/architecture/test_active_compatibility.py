from scripts.maintenance.check_active_compatibility import scan


def test_current_active_code_has_no_retired_compatibility():
    assert scan() == []


def test_scanner_reports_active_marker_but_ignores_migration(tmp_path):
    active = tmp_path / "apps/execution-service"
    active.mkdir(parents=True)
    (active / "bad.py").write_text("LEGACY_WORKFLOW_BRIDGE_PREFIX = '/internal/'\n")
    migration = (
        tmp_path
        / "apps/execution-service/src/platform_execution/migrations/versions/0001_execution.py"
    )
    migration.parent.mkdir(parents=True)
    migration.write_text("legacy_v1 = 'historical database value'\n")
    assert scan(tmp_path) == [
        {
            "code": "MISSING_RELEASE_DIGEST_BYPASS",
            "path": "apps/execution-service/bad.py",
        }
    ]
