import ast
from pathlib import Path

import platform_infrastructure.repositories as repositories

from scripts.maintenance.check_architecture_boundaries import _repository_table_violations

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = ROOT / "packages/platform-infrastructure/src/platform_infrastructure/repositories"
DOMAIN_MODULES = {
    "AccessRepository": "access",
    "ActionRepository": "actions",
    "AuthoringRepository": "authoring",
    "BudgetRepository": "budgets",
    "DeliveryRepository": "delivery",
    "ResearchRepository": "research",
    "ScienceRepository": "science",
    "ServiceBindingRepository": "service_bindings",
    "TaskRepository": "tasks",
    "WorkflowRepository": "workflows",
    "AgentRunLeaseRepository": "agent_runs",
    "AgentConversationRepository": "agent_conversations",
    "AgentToolRepository": "agent_tools",
    "AgentBudgetRepository": "agent_budgets",
    "AgentRecoveryRepository": "agent_recovery",
}


def test_repository_public_api_is_backed_by_responsibility_modules():
    assert not (REPOSITORY_ROOT.parent / "repositories.py").exists()
    for name, module in DOMAIN_MODULES.items():
        repository = getattr(repositories, name)
        assert repository.__module__ == f"platform_infrastructure.repositories.{module}"
        assert (REPOSITORY_ROOT / f"{module}.py").is_file()
    assert not hasattr(repositories, "AgentRepository")


def test_active_code_has_no_legacy_agent_repository_access():
    offenders = []
    for root in (ROOT / "packages", ROOT / "apps", ROOT / "tests"):
        for path in root.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "uow"
                    and node.attr == "agents"
                ):
                    offenders.append(path.relative_to(ROOT).as_posix())
    assert not offenders


def test_repositories_use_only_their_aggregate_table_modules():
    assert _repository_table_violations(ROOT) == []


def test_repository_table_gate_rejects_the_legacy_aggregate_import(tmp_path: Path):
    path = (
        tmp_path
        / "packages/platform-infrastructure/src/platform_infrastructure/repositories/access.py"
    )
    path.parent.mkdir(parents=True)
    path.write_text("from platform_infrastructure import tables as t\n", encoding="utf-8")

    assert _repository_table_violations(tmp_path) == [
        {
            "code": "REPOSITORY_TABLE_AGGREGATE_IMPORT",
            "path": (
                "packages/platform-infrastructure/src/platform_infrastructure/"
                "repositories/access.py"
            ),
            "target": "platform_infrastructure.tables",
        }
    ]
