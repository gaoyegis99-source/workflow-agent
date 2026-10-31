"""Local privileged administration. Tokens are printed only when explicitly issued."""

import argparse
import json
import sys

from platform_core.application.catalog import load_catalog
from platform_core.application.operator_proposals import OperatorProposalMaintainer
from platform_core.domain.control import RuleViolation
from sqlalchemy.exc import SQLAlchemyError

from platform_infrastructure.composition import PlatformAdminComposition
from platform_infrastructure.database import Database


def main():
    parser = argparse.ArgumentParser(description="V7 local database administration")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate")
    commands.add_parser("check-schema")
    principal = commands.add_parser("principal-create")
    principal.add_argument("--name", required=True)
    bind_subject = commands.add_parser("principal-bind-subject")
    bind_subject.add_argument("--principal-id", required=True)
    bind_subject.add_argument("--subject", required=True)
    bind_subject.add_argument("--reviewer", required=True)
    bind_subject.add_argument("--reason", required=True)
    issue = commands.add_parser("token-issue")
    issue.add_argument("--principal-id", required=True)
    issue.add_argument("--ttl-seconds", type=int, default=2592000)
    commands.add_parser("token-revoke").add_argument("--token-id", required=True)
    member = commands.add_parser("member-set")
    member.add_argument("--project-id", required=True)
    member.add_argument("--principal-id", required=True)
    member.add_argument("--role", choices=["OWNER", "EDITOR", "VIEWER", "REMOVE"], required=True)
    for command in ("service-binding-create", "service-binding-revise"):
        binding = commands.add_parser(command)
        binding.add_argument("--project-id", required=True)
        binding.add_argument("--service-binding-id")
        binding.add_argument("--file", required=True)
    for command in ("service-binding-enable", "service-binding-disable"):
        binding = commands.add_parser(command)
        binding.add_argument("--project-id", required=True)
        binding.add_argument("--service-binding-id", required=True)
    review = commands.add_parser("operator-proposal-review")
    review.add_argument("--project-id", required=True)
    review.add_argument("--proposal-id", required=True)
    review.add_argument("--revision", type=int, required=True)
    review.add_argument(
        "--decision", choices=["APPROVED", "CHANGES_REQUESTED", "REJECTED"], required=True
    )
    review.add_argument("--maintainer-id", required=True)
    review.add_argument("--evidence-digest", required=True)
    review.add_argument("--reason", required=True)
    deployment = commands.add_parser("operator-proposal-deployment-import")
    deployment.add_argument("--project-id", required=True)
    deployment.add_argument("--proposal-id", required=True)
    deployment.add_argument("--revision", type=int, required=True)
    deployment.add_argument("--maintainer-id", required=True)
    deployment.add_argument("--evidence-digest", required=True)
    args = parser.parse_args()
    database = None
    try:
        composition = PlatformAdminComposition.from_environment()
        database = Database(composition.database_url)
        result = {"status": "ok"}
        if args.command == "migrate":
            database.migrate()
        else:
            database.check_schema()
            if args.command == "principal-create":
                result = {"principal_id": database.create_principal(args.name)}
            elif args.command == "principal-bind-subject":
                result = database.bind_subject(
                    args.principal_id,
                    args.subject,
                    reviewer=args.reviewer,
                    reason=args.reason,
                )
            elif args.command == "token-issue":
                result = database.issue_token(args.principal_id, args.ttl_seconds)
            elif args.command == "token-revoke":
                database.revoke_token(args.token_id)
            elif args.command == "member-set":
                actor = database.authenticate(composition.owner_token)
                database.set_member(args.project_id, args.principal_id, args.role, actor=actor)
            elif args.command in {"service-binding-create", "service-binding-revise"}:
                with open(args.file, encoding="utf-8") as stream:
                    body = json.load(stream)
                result = (
                    database.service_binding_create(args.project_id, body)
                    if args.command == "service-binding-create"
                    else database.service_binding_revise(
                        args.project_id, args.service_binding_id, body
                    )
                )
            elif args.command in {"service-binding-enable", "service-binding-disable"}:
                result = database.service_binding_enable(
                    args.project_id, args.service_binding_id, args.command.endswith("enable")
                )
            elif args.command in {
                "operator-proposal-review",
                "operator-proposal-deployment-import",
            }:
                operator_path = composition.operator_catalog_path
                revision = composition.deployment.deployment_revision
                if operator_path is None or not revision:
                    raise RuntimeError(
                        "Maintainer proposal commands require an explicit operator catalog "
                        "and direct deployment revision"
                    )
                catalog = load_catalog(operator_path)
                deployment_evidence = {
                    # The persistent receipt field keeps its old wire name for
                    # historical reads, but this is not a Release Lock.
                    "release_digest": revision,
                    "operator_catalog": {"digest": catalog["catalog_digest"]},
                    "source_tree": {
                        "digest": composition.deployment_source_digest or "UNAVAILABLE"
                    },
                }
                if (
                    args.command == "operator-proposal-deployment-import"
                    and composition.deployment_source_digest is None
                ):
                    raise RuntimeError(
                        "Deployment import requires V7_DEPLOYMENT_SOURCE_DIGEST; "
                        "direct Compose never infers source identity"
                    )
                maintainer = OperatorProposalMaintainer(
                    database, operator_catalog=catalog, release_lock=deployment_evidence
                )
                if args.command == "operator-proposal-review":
                    result = maintainer.review(
                        args.project_id,
                        args.proposal_id,
                        args.revision,
                        args.decision,
                        args.maintainer_id,
                        args.evidence_digest,
                        args.reason,
                    )
                else:
                    result = maintainer.import_deployment(
                        args.project_id,
                        args.proposal_id,
                        args.revision,
                        args.maintainer_id,
                        args.evidence_digest,
                    )
        print(json.dumps(result, default=str))
    except RuleViolation as error:
        print(json.dumps({"code": error.code, "message": error.message}), file=sys.stderr)
        raise SystemExit(1) from None
    except (SQLAlchemyError, RuntimeError, ValueError):
        print(
            "Database unavailable or migration required; check local configuration", file=sys.stderr
        )
        raise SystemExit(1) from None
    finally:
        if database:
            database.close()
