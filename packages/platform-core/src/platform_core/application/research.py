"""P5.2 immutable research protocols, claims, provenance and policy decisions."""

from platform_core.application.control import ControlService
from platform_core.domain.control import require
from platform_core.domain.research import digest, evaluate_groundwater_comparison


def instant(value):
    return value.isoformat()


def policy_view(row):
    return {
        "decision_id": row["decision_id"],
        "policy_id": row["policy_id"],
        "policy_version": row["policy_version"],
        "policy_digest": row["policy_digest"],
        "input_digest": row["input_digest"],
        "facts_used": row["facts_used"],
        "result": row["result"],
        "reasons": row["reasons"],
        "evaluated_at": instant(row["evaluated_at"]),
    }


def freeze_research_context(uow, project, task, context):
    """Validate a protocol link at action admission and return its immutable decision facts."""

    if context is None:
        return None
    registration = uow.research.registration(
        project, task.task_id, context["analysis_registration_id"]
    )
    require(
        registration["task_spec_revision"] == task.spec_revision,
        "REVISION_CONFLICT",
        "Analysis registration targets an older task specification",
    )
    require(
        uow.research.successor(registration["revision_id"]) is None,
        "REVISION_CONFLICT",
        "Analysis registration has been superseded",
    )
    decision = uow.research.policy_decision(registration["policy_decision_id"])
    requirement = context.get("policy_requirement")
    if requirement is not None:
        require(
            decision["policy_id"] == requirement and decision["result"] == "APPLICABLE",
            "PRECONDITION_UNRESOLVED",
            "The declared action policy has unresolved missing or conflicting facts",
        )
    return {
        "analysis_registration_id": registration["registration_id"],
        "analysis_revision_id": registration["revision_id"],
        "policy_requirement": requirement,
        "policy_decision_id": decision["decision_id"],
        "policy_id": decision["policy_id"],
        "policy_version": decision["policy_version"],
        "policy_digest": decision["policy_digest"],
        "policy_result": decision["result"],
    }


class Research:
    def __init__(self, store):
        self.store = store
        self.control = ControlService(store)

    def authorize(self, actor, project, role="VIEWER"):
        with self.store.transaction() as uow:
            uow.access.authorize(actor, project, role)

    @staticmethod
    def _policy(uow, project, task_id, comparison):
        evaluated = evaluate_groundwater_comparison(comparison)
        row = evaluated | {
            "decision_id": uow.new_id(),
            "project_id": project,
            "task_id": task_id,
            "evaluated_at": uow.now(),
        }
        uow.research.add_policy_decision(row)
        return row

    @staticmethod
    def _source_fact(uow, project, task_id, source, resolved, now):
        kind = source["kind"]
        fact = {"source": source, "observed_at": instant(now)}
        if kind == "ASSET":
            require(
                source["ref"]["project_id"] == project,
                "SCOPE_DENIED",
                "Cross-project source denied",
                403,
            )
            require(
                resolved and resolved.get("verified") is True,
                "INVALID_BINDING",
                "Asset source was not verified",
            )
        elif kind == "EVIDENCE":
            require(
                source["ref"]["project_id"] == project,
                "SCOPE_DENIED",
                "Cross-project source denied",
                403,
            )
            require(
                resolved and resolved.get("verified") is True and resolved.get("read_receipt_id"),
                "INVALID_BINDING",
                "Evidence source was not exactly read",
            )
            require(
                resolved.get("availability", "ACTIVE") == "ACTIVE",
                "PRECONDITION_UNRESOLVED",
                "Withdrawn evidence cannot support a new research record",
            )
            fact["read_receipt_id"] = resolved["read_receipt_id"]
            for name in (
                "lifecycle_revision",
                "availability",
                "epistemic_status",
                "review_state",
            ):
                if name in resolved:
                    fact[name] = resolved[name]
        elif kind == "EXECUTION":
            action = uow.actions.by_execution(project, source["execution_id"])
            require(
                action["task_id"] == task_id,
                "SCOPE_DENIED",
                "An execution from another task cannot be used as this task's evidence",
                403,
            )
            observation = action.get("last_external_observation") or {}
            fact.update(
                action_id=action["action_id"], state=observation.get("state", action["state"])
            )
        elif kind == "METHOD":
            require(
                resolved and resolved.get("verified") is True,
                "INVALID_BINDING",
                "Method source was not verified",
            )
        else:
            require(False, "INVALID_BINDING", "Unsupported provenance source")
        return fact

    @staticmethod
    def _relation(uow, project, task_id, subject, source, actor, role, relation, kind, now):
        body = {
            "project_id": project,
            "task_id": task_id,
            "relation": relation,
            "provenance_kind": kind,
            "subject": subject,
            "object": source,
            "activity_execution_id": (
                source["execution_id"] if source["kind"] == "EXECUTION" else None
            ),
            "agent_id": actor.principal_id,
            "role": role,
        }
        uow.research.add_provenance(
            body
            | {
                "relation_id": uow.new_id(),
                "recorded_at": now,
                "relation_digest": digest(body),
            }
        )

    @staticmethod
    def _analysis_status(uow, row):
        if uow.research.successor(row["revision_id"]):
            return "SUPERSEDED", None
        registration = uow.research.registration_for_revision(row["revision_id"])
        return (
            ("REGISTERED", registration["registration_id"])
            if registration
            else (
                "EXPLORATORY",
                None,
            )
        )

    @classmethod
    def _analysis_view(cls, uow, row):
        status, registration_id = cls._analysis_status(uow, row)
        public = {key: value for key, value in row["payload"].items() if not key.startswith("_")}
        return public | {
            "analysis_id": row["analysis_id"],
            "revision_id": row["revision_id"],
            "revision": row["revision"],
            "task_id": row["task_id"],
            "task_spec_revision": row["task_spec_revision"],
            "status": status,
            "registration_id": registration_id,
            "content_digest": row["content_digest"],
            "policy_decision": policy_view(uow.research.policy_decision(row["policy_decision_id"])),
            "created_by": row["created_by"],
            "created_at": instant(row["created_at"]),
        }

    @staticmethod
    def _registration_view(uow, row):
        return {
            "registration_id": row["registration_id"],
            "analysis_id": row["analysis_id"],
            "revision_id": row["revision_id"],
            "revision": row["revision"],
            "task_id": row["task_id"],
            "task_spec_revision": row["task_spec_revision"],
            "observation_watermark": row["observation_watermark"],
            "watermark_digest": row["watermark_digest"],
            "policy_decision": policy_view(uow.research.policy_decision(row["policy_decision_id"])),
            "registered_by": row["registered_by"],
            "registered_at": instant(row["registered_at"]),
        }

    @staticmethod
    def _claim_view(row):
        return row["payload"] | {
            "claim_id": row["claim_id"],
            "claim_revision_id": row["claim_revision_id"],
            "revision": row["revision"],
            "task_id": row["task_id"],
            "supersedes_claim_revision_id": row["supersedes_claim_revision_id"],
            "content_digest": row["content_digest"],
            "created_by": row["created_by"],
            "created_at": instant(row["created_at"]),
        }

    def create_analysis(self, actor, project, task_id, key, request, resolved_sources):
        def create(uow):
            task = uow.tasks.get(project, task_id, lock=True)
            task.open()
            require(
                task.spec_revision == request["expected_spec_revision"],
                "REVISION_CONFLICT",
                "Task requirements changed",
            )
            previous_id = request.get("supersedes_revision_id")
            if previous_id:
                previous = uow.research.analysis(project, task_id, previous_id, lock=True)
                require(
                    uow.research.successor(previous_id) is None,
                    "REVISION_CONFLICT",
                    "Analysis revision already has a successor",
                )
                analysis_id, revision = previous["analysis_id"], previous["revision"] + 1
            else:
                analysis_id, revision = uow.new_id(), 1
            now = uow.now()
            policy = self._policy(uow, project, task_id, request.get("groundwater_comparison"))
            revision_id = uow.new_id()
            stored = request.copy()
            subject = {"kind": "ANALYSIS_REVISION", "object_id": revision_id, "revision": revision}
            resolved_iter = iter(resolved_sources)
            relations = []
            observed_facts = []
            for role in request["input_roles"]:
                source = role["source"]
                self._source_fact(uow, project, task_id, source, next(resolved_iter, None), now)
                relations.append((source, role["role"], "USED", "DECLARED_PROVENANCE"))
            for method in request["methods"]:
                source = {"kind": "METHOD", "ref": method}
                self._source_fact(uow, project, task_id, source, next(resolved_iter, None), now)
                relations.append((source, "method", "USED", "DECLARED_PROVENANCE"))
            for source in request["observed_sources"]:
                observed_facts.append(
                    self._source_fact(uow, project, task_id, source, next(resolved_iter, None), now)
                )
                relations.append((source, "observation", "USED", "OBSERVED_PROVENANCE"))
            comparison = request.get("groundwater_comparison")
            for series in comparison["series"] if comparison else []:
                for name in ("elevation_conversion", "datum_conversion"):
                    conversion = series.get(name)
                    if not conversion:
                        continue
                    source = conversion["source"]
                    self._source_fact(uow, project, task_id, source, next(resolved_iter, None), now)
                    relations.append((source, name, "SUPPORTED_BY", "DECLARED_PROVENANCE"))
            require(next(resolved_iter, None) is None, "INVALID_BINDING", "Source facts differ")
            record = {
                "revision_id": revision_id,
                "analysis_id": analysis_id,
                "revision": revision,
                "project_id": project,
                "task_id": task_id,
                "task_spec_revision": task.spec_revision,
                "supersedes_revision_id": previous_id,
                # Recovery-only observations are frozen in the same insert as the public
                # protocol. They are omitted from the public view and its content digest.
                "payload": stored | {"_observed_facts": observed_facts},
                "content_digest": digest(stored),
                "policy_decision_id": policy["decision_id"],
                "created_by": actor.principal_id,
                "created_at": now,
            }
            uow.research.add_analysis(record)
            for source, role, relation, provenance_kind in relations:
                self._relation(
                    uow,
                    project,
                    task_id,
                    subject,
                    source,
                    actor,
                    role,
                    relation,
                    provenance_kind,
                    now,
                )
            return self._analysis_view(uow, record)

        payload = request | {"task_id": task_id}
        return self.control._write(
            actor, project, "create_analysis", key, payload, "EDITOR", create
        )

    def register(self, actor, project, task_id, revision_id, key, request):
        def register(uow):
            task = uow.tasks.get(project, task_id, lock=True)
            task.open()
            require(
                task.spec_revision == request["expected_spec_revision"],
                "REVISION_CONFLICT",
                "Task requirements changed",
            )
            analysis = uow.research.analysis(project, task_id, revision_id, lock=True)
            require(
                analysis["task_spec_revision"] == task.spec_revision,
                "REVISION_CONFLICT",
                "Analysis targets an older task specification",
            )
            require(
                uow.research.successor(revision_id) is None,
                "REVISION_CONFLICT",
                "A superseded analysis cannot be registered",
            )
            require(
                uow.research.registration_for_revision(revision_id) is None,
                "PRECONDITION_UNRESOLVED",
                "Analysis is already registered",
            )
            now = uow.now()
            watermark = list(analysis["payload"].get("_observed_facts", []))
            known = {digest(item["source"]) for item in watermark}
            for action in uow.actions.list(task_id):
                if not action.get("execution_id"):
                    continue
                source = {"kind": "EXECUTION", "execution_id": action["execution_id"]}
                if digest(source) in known:
                    continue
                watermark.append(
                    {
                        "source": source,
                        "observed_at": instant(now),
                        "action_id": action["action_id"],
                        "state": action["state"],
                    }
                )
                self._relation(
                    uow,
                    project,
                    task_id,
                    {
                        "kind": "ANALYSIS_REVISION",
                        "object_id": revision_id,
                        "revision": analysis["revision"],
                    },
                    source,
                    actor,
                    "pre-registration-observation",
                    "USED",
                    "OBSERVED_PROVENANCE",
                    now,
                )
            watermark.sort(key=lambda item: digest(item["source"]))
            policy = self._policy(
                uow, project, task_id, analysis["payload"].get("groundwater_comparison")
            )
            record = {
                "registration_id": uow.new_id(),
                "project_id": project,
                "task_id": task_id,
                "analysis_id": analysis["analysis_id"],
                "revision_id": revision_id,
                "revision": analysis["revision"],
                "task_spec_revision": task.spec_revision,
                "observation_watermark": watermark,
                "watermark_digest": digest(watermark),
                "policy_decision_id": policy["decision_id"],
                "registered_by": actor.principal_id,
                "registered_at": now,
            }
            uow.research.add_registration(record)
            return self._registration_view(uow, record)

        return self.control._write(
            actor,
            project,
            "register_analysis",
            key,
            request | {"task_id": task_id, "revision_id": revision_id},
            "EDITOR",
            register,
        )

    def create_claim(self, actor, project, task_id, key, request, resolved_sources):
        def create(uow):
            task = uow.tasks.get(project, task_id, lock=True)
            task.open()
            require(
                task.spec_revision == request["expected_spec_revision"],
                "REVISION_CONFLICT",
                "Task requirements changed",
            )
            registration = uow.research.registration(
                project, task_id, request["analysis_registration_id"]
            )
            require(
                registration["task_spec_revision"] == task.spec_revision,
                "REVISION_CONFLICT",
                "Registration targets an older task specification",
            )
            require(
                uow.research.successor(registration["revision_id"]) is None,
                "REVISION_CONFLICT",
                "Registration has been superseded",
            )
            claim_id = request.get("claim_id")
            if claim_id:
                previous = uow.research.claim_revision(
                    project,
                    task_id,
                    claim_id,
                    request["expected_claim_revision"],
                    lock=True,
                )
                later = [
                    value
                    for value in uow.research.claims(task_id)
                    if value["claim_id"] == claim_id and value["revision"] > previous["revision"]
                ]
                require(not later, "REVISION_CONFLICT", "Claim revision is not current")
                revision = previous["revision"] + 1
                supersedes = previous["claim_revision_id"]
            else:
                require(
                    request["support_status"] != "WITHDRAWN",
                    "MALFORMED_REQUEST",
                    "A new claim cannot start withdrawn",
                    400,
                )
                claim_id, revision, supersedes = uow.new_id(), 1, None
            now = uow.now()
            resolved_iter = iter(resolved_sources)
            for use in request["sources"]:
                self._source_fact(
                    uow,
                    project,
                    task_id,
                    use["source"],
                    next(resolved_iter, None),
                    now,
                )
            require(next(resolved_iter, None) is None, "INVALID_BINDING", "Source facts differ")
            if request["proposition_type"] == "CAUSAL":
                require(
                    bool(request["limitations"]),
                    "PRECONDITION_UNRESOLVED",
                    "A causal proposal must state its limitations",
                )
            if request["support_status"] == "SUPPORTED":
                require(
                    any(value["relation"] == "SUPPORTED_BY" for value in request["sources"]),
                    "PRECONDITION_UNRESOLVED",
                    "A supported claim requires an exact supporting source",
                )
            stored = request.copy()
            claim_revision_id = uow.new_id()
            record = {
                "claim_revision_id": claim_revision_id,
                "claim_id": claim_id,
                "revision": revision,
                "project_id": project,
                "task_id": task_id,
                "task_spec_revision": task.spec_revision,
                "analysis_registration_id": registration["registration_id"],
                "supersedes_claim_revision_id": supersedes,
                "payload": stored,
                "content_digest": digest(stored),
                "created_by": actor.principal_id,
                "created_at": now,
            }
            uow.research.add_claim(record)
            subject = {
                "kind": "CLAIM_REVISION",
                "object_id": claim_revision_id,
                "revision": revision,
            }
            for use in request["sources"]:
                self._relation(
                    uow,
                    project,
                    task_id,
                    subject,
                    use["source"],
                    actor,
                    use["role"],
                    use["relation"],
                    "OBSERVED_PROVENANCE",
                    now,
                )
            return self._claim_view(record)

        return self.control._write(
            actor,
            project,
            "create_claim",
            key,
            request | {"task_id": task_id},
            "EDITOR",
            create,
        )

    def view(self, actor, project, task_id):
        with self.store.transaction() as uow:
            uow.access.authorize(actor, project)
            uow.tasks.get(project, task_id)
            analyses = [self._analysis_view(uow, row) for row in uow.research.analyses(task_id)]
            registrations = [
                self._registration_view(uow, row) for row in uow.research.registrations(task_id)
            ]
            claims = [self._claim_view(row) for row in uow.research.claims(task_id)]
            provenance = [
                {
                    "relation_id": row["relation_id"],
                    "relation": row["relation"],
                    "provenance_kind": row["provenance_kind"],
                    "subject": row["subject"],
                    "object": row["object"],
                    "activity_execution_id": row["activity_execution_id"],
                    "agent_id": row["agent_id"],
                    "role": row["role"],
                    "recorded_at": instant(row["recorded_at"]),
                }
                for row in uow.research.provenance(task_id)
            ]
            decisions = [policy_view(row) for row in uow.research.policy_decisions(task_id)]
            missing = []
            seen = set()
            for decision in decisions:
                if decision["result"] not in {"MISSING", "CONFLICT"}:
                    continue
                for item in decision["reasons"]:
                    identity = digest(item)
                    if identity not in seen:
                        missing.append(item)
                        seen.add(identity)
            return {
                "task_id": task_id,
                "analysis_revisions": analyses,
                "registrations": registrations,
                "claims": claims,
                "provenance": provenance,
                "policy_decisions": decisions,
                "missing": missing,
            }
