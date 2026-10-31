"""Keep authoring edit and graph-analysis responsibilities physically separate."""

import ast

import platform_core.application.authoring_release as release
from platform_core.application.trial_verdict import TrialVerdictProcessor

from scripts.maintenance.check_architecture_boundaries import _authoring_graph_violations
from tests.pathing import SOURCE_ROOT

DOMAIN = SOURCE_ROOT / "packages/platform-core/src/platform_core/domain"


def test_trial_verdict_worker_is_not_reintroduced_into_release_editing_module():
    assert TrialVerdictProcessor.__module__.endswith("trial_verdict")
    assert not hasattr(release, "TrialVerdictProcessor")


def test_authoring_graph_public_module_is_a_controlled_facade():
    facade = DOMAIN / "authoring_graph.py"
    source = facade.read_text(encoding="utf-8")
    tree = ast.parse(source)

    assert len(source.splitlines()) <= 40
    assert not any(isinstance(node, ast.FunctionDef) for node in tree.body)
    assert {
        "authoring_graph_envelope.py",
        "authoring_graph_structure.py",
        "authoring_graph_preview.py",
        "authoring_graph_advanced.py",
        "authoring_graph_staleness.py",
    } <= {path.name for path in DOMAIN.glob("authoring_graph_*.py")}


def test_authoring_graph_responsibility_modules_do_not_back_import_the_facade():
    for path in DOMAIN.glob("authoring_graph_*.py"):
        source = path.read_text(encoding="utf-8")
        assert "authoring_graph import" not in source, path.name
        assert "from .authoring_graph import" not in source, path.name
    assert _authoring_graph_violations(SOURCE_ROOT) == []
