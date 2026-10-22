"""Bounded Agent operations; no command can alter scientific outcomes."""

import argparse
import json
from datetime import date, datetime

from platform_core.application.action_reconciliation import AgentActionReconciler
from platform_core.application.agent_kernel import AgentCoordinator
from platform_core.application.agent_waiting import AgentWaitReconciler
from platform_infrastructure.database import Database


def serial(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: serial(item) for key, item in value.items()}
    if isinstance(value, list):
        return [serial(item) for item in value]
    return value


def list_runs(database, blocked_only=False):
    with database.transaction() as uow:
        return uow.agents.blocked_runs() if blocked_only else uow.agents.waiting_runs()


def export_run(database, agent_run_id):
    with database.transaction() as uow:
        run = uow.agents.run_by_id(agent_run_id)
        return {
            "run": run,
            "plans": uow.agents.plans(agent_run_id),
            "events": uow.agents.events(agent_run_id),
            "wakeups": uow.agents.wakeups(agent_run_id),
            "wait_dependencies": uow.agents.wait_dependencies(agent_run_id),
            "runtime_recoveries": uow.agents.runtime_recoveries(agent_run_id),
            "delivery_attempts": uow.agents.delivery_attempts(agent_run_id),
        }


def main():
    parser = argparse.ArgumentParser(description="Inspect and reconcile the V7 Agent ledger")
    parser.add_argument(
        "command",
        choices=("list-waiting", "list-blocked", "reconcile", "export", "rebuild-checkpoint"),
    )
    parser.add_argument("--agent-run-id")
    args = parser.parse_args()
    database = Database.from_environment()
    try:
        database.check_schema()
        if args.command == "reconcile":
            value = {
                "actions_changed": AgentActionReconciler(database).process(),
                "waits_changed": AgentWaitReconciler(database).process(),
            }
        elif args.command == "export":
            if not args.agent_run_id:
                raise SystemExit("--agent-run-id is required for export")
            value = export_run(database, args.agent_run_id)
        elif args.command == "rebuild-checkpoint":
            if not args.agent_run_id:
                raise SystemExit("--agent-run-id is required for rebuild-checkpoint")
            value = AgentCoordinator(database).request_checkpoint_rebuild(args.agent_run_id)
        else:
            value = list_runs(database, blocked_only=args.command == "list-blocked")
        print(json.dumps(serial(value), ensure_ascii=False, sort_keys=True))
    finally:
        database.close()
