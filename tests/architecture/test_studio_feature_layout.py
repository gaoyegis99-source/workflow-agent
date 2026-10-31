from tests.pathing import SOURCE_ROOT

STUDIO = SOURCE_ROOT / "apps/capability-studio/src/features/governance/studio"
TASKS = SOURCE_ROOT / "apps/capability-studio/src/features/tasks"


def test_large_feature_entrypoints_delegate_display_only_projections() -> None:
    workflow = (STUDIO / "WorkflowStudio.tsx").read_text(encoding="utf-8")
    releases = (STUDIO / "workflowStudioReleaseGroups.ts").read_text(encoding="utf-8")
    tasks = (TASKS / "index.tsx").read_text(encoding="utf-8")
    rows = (TASKS / "taskRows.ts").read_text(encoding="utf-8")

    assert "groupWorkflowReleases" in workflow
    assert "function groupWorkflowReleases" in releases
    assert "projectTaskRows" in tasks
    assert "function projectTaskRows" in rows


def test_frontend_helpers_remain_feature_local() -> None:
    for path in (STUDIO / "workflowStudioReleaseGroups.ts", TASKS / "taskRows.ts"):
        source = path.read_text(encoding="utf-8")
        assert "../../../../" not in source
        assert "../.." not in source or "shared" in source
