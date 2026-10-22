"""P6.1 deterministic Agent ledger and coordination use cases."""

from __future__ import annotations

from dataclasses import asdict

from platform_core.application.agent_waiting import (
    dependency_fact,
    persist_wait_dependencies,
    wake_run,
)
from platform_core.application.control import digest
from platform_core.application.ports import Record, Store, UnitOfWork
from platform_core.domain.agent_kernel import (
    AgentLeaseClaim,
    outcome_state,
    require_agent_task_active,
)
from platform_core.domain.agent_runtime import DecisionFrame, DecisionProposal, canonical_digest
from platform_core.domain.control import Identity, require, valid_identifier


def create_agent_run(
    uow: UnitOfWork,
    task,
    actor: Identity,
    runtime_profile: Record,
    *,
    model_profile: Record | None = None,
    model_budget: Record | None = None,
    tool_catalog: Record | None = None,
    operator_catalog: Record | None = None,
) -> Record:
    """Create the business ledger entry in the same transaction as the Task."""
    profile_digest = runtime_profile.get("profile_digest")
    require(
        valid_identifier(runtime_profile.get("profile_id"))
        and isinstance(profile_digest, str)
        and len(profile_digest) == 71
        and profile_digest.startswith("sha256:"),
        "INVALID_BINDING",
        "The Agent runtime profile is not frozen",
        422,
    )
    canonical_digest(runtime_profile)
    if tool_catalog is not None or operator_catalog is not None:
        require(
            isinstance(tool_catalog, dict)
            and tool_catalog.get("catalog_id") in {"p7-action-tools-v2", "p7-science-tools-v2"}
            and isinstance(tool_catalog.get("catalog_digest"), str)
            and isinstance(operator_catalog, dict)
            and isinstance(operator_catalog.get("catalog_digest"), str),
            "INVALID_BINDING",
            "New Agent runs require the P7 tool and operator catalogues",
            422,
        )
        unsigned = dict(runtime_profile) | {
            "tool_catalog_id": tool_catalog["catalog_id"],
            "tool_catalog_digest": tool_catalog["catalog_digest"],
            "operator_catalog_digest": operator_catalog["catalog_digest"],
        }
        runtime_profile = unsigned | {"deployment_profile_digest": canonical_digest(unsigned)}
        profile_digest = runtime_profile["deployment_profile_digest"]
    now = uow.now()
    agent_run_id = uow.new_id()
    record = {
        "agent_run_id": agent_run_id,
        "project_id": task.project_id,
        "task_id": task.task_id,
        "status": "READY",
        "epoch": 1,
        "generation": 1,
        "runtime_ref": f"{agent_run_id}-e1",
        "runtime_profile": runtime_profile,
        "runtime_profile_digest": profile_digest,
        "task_spec_revision": task.spec_revision,
        "control_version": task.control_version,
        "plan_revision": 0,
        "frame_sequence": 0,
        "accepted_proposals": 0,
        "wake_revision": 0,
        "blocked_reason": None,
        "created_by": actor.principal_id,
        "created_at": now,
        "updated_at": now,
    }
    uow.agents.add_run(record)
    require(
        (model_profile is None) == (model_budget is None),
        "INVALID_BINDING",
        "Model profile and budget must be configured together",
        422,
    )
    if model_profile is not None:
        from platform_core.domain.model_gateway import ModelBudgetLimits

        limits = ModelBudgetLimits.from_record(model_budget)
        uow.agents.add_model_budget(
            {
                "agent_run_id": agent_run_id,
                "task_id": task.task_id,
                "profile_id": model_profile["profile_id"],
                "profile_digest": model_profile["profile_digest"],
                **limits.as_record(),
                "used_calls": 0,
                "reserved_calls": 0,
                "used_input_tokens": 0,
                "reserved_input_tokens": 0,
                "used_output_tokens": 0,
                "reserved_output_tokens": 0,
                "used_reasoning_tokens": 0,
                "reserved_reasoning_tokens": 0,
                "used_cost_microunits": 0,
                "reserved_cost_microunits": 0,
                "created_at": now,
            }
        )
    uow.agents.add_outbox(
        {
            "event_id": uow.new_id(),
            "agent_run_id": agent_run_id,
            "kind": "AGENT_RUN_CREATED",
            "payload": {
                "agent_run_id": agent_run_id,
                "task_id": task.task_id,
                "task_spec_revision": task.spec_revision,
            },
            "done": False,
            "available_at": now,
            "owner": None,
            "lease_until": None,
            "generation": 0,
        }
    )
    return record


def task_execution_mode(uow: UnitOfWork, task_id: str) -> str:
    return "AGENT" if uow.agents.run_by_task(task_id) is not None else "DIRECT"


def apply_task_change(uow: UnitOfWork, task, kind: str) -> None:
    """Fence any in-flight Agent decision when authoritative Task state changes."""
    run = uow.agents.run_by_task(task.task_id, lock=True)
    if run is None:
        return
    now = uow.now()
    uow.agents.stale_pending_frames(run["agent_run_id"])
    uow.agents.revoke_lease(run["agent_run_id"])
    generation = run["generation"] + 1
    changes = {
        "generation": generation,
        "task_spec_revision": task.spec_revision,
        "control_version": task.control_version,
        "updated_at": now,
        "blocked_reason": None,
    }
    if task.lifecycle == "CANCELLED":
        changes["status"] = "STOPPED"
        changes["blocked_reason"] = "TASK_CANCELLED"
        uow.agents.cancel_questions(run["agent_run_id"])
        uow.agents.cancel_wakeups(run["agent_run_id"])
        uow.agents.cancel_wait_dependencies(run["agent_run_id"], now)
        uow.agents.clear_reconciliation(run["agent_run_id"])
    elif task.control == "PAUSED":
        changes["status"] = "WAITING"
        changes["blocked_reason"] = "TASK_PAUSED"
    else:
        revision = run["wake_revision"] + 1
        event_id = uow.new_id()
        uow.agents.add_wakeup(
            {
                "wake_id": uow.new_id(),
                "agent_run_id": run["agent_run_id"],
                "revision": revision,
                "reason": "TASK",
                "event_ids": [event_id],
                "state": "PENDING",
                "created_at": now,
                "consumed_at": None,
            }
        )
        changes.update(status="READY", wake_revision=revision)
        uow.agents.add_outbox(
            {
                "event_id": event_id,
                "agent_run_id": run["agent_run_id"],
                "kind": kind,
                "payload": {
                    "task_id": task.task_id,
                    "spec_revision": task.spec_revision,
                    "control_version": task.control_version,
                },
                "done": False,
                "available_at": now,
                "owner": None,
                "lease_until": None,
                "generation": 0,
            }
        )
    uow.agents.update_run(run["agent_run_id"], **changes)


class AgentCoordinator:
    """Owns Agent progression facts; external runtime calls stay outside its transactions."""

    def __init__(self, store: Store, context_compiler=None):
        self.store = store
        self.context_compiler = context_compiler

    def claim(self, owner: str, lease_seconds: float = 30) -> AgentLeaseClaim | None:
        require(valid_identifier(owner), "INVALID_BINDING", "Invalid Agent worker identity", 422)
        require(lease_seconds > 0, "MALFORMED_REQUEST", "Lease duration must be positive", 400)
        with self.store.transaction() as uow:
            record = uow.agents.claim(owner, lease_seconds)
            if record is None:
                return None
            return AgentLeaseClaim(
                agent_run_id=record["agent_run_id"],
                project_id=record["project_id"],
                task_id=record["task_id"],
                runtime_ref=record["runtime_ref"],
                epoch=record["epoch"],
                generation=record["generation"],
                owner=owner,
                task_spec_revision=record["task_spec_revision"],
                control_version=record["control_version"],
                runtime_profile=record["runtime_profile"],
            )

    def renew(self, claim: AgentLeaseClaim, lease_seconds: float = 30) -> bool:
        with self.store.transaction() as uow:
            return uow.agents.renew_lease(
                claim.agent_run_id, claim.owner, claim.generation, lease_seconds
            )

    @staticmethod
    def _require_owned(uow: UnitOfWork, claim: AgentLeaseClaim) -> Record:
        run = uow.agents.run(claim.project_id, claim.agent_run_id, lock=True)
        require(
            uow.agents.lease_owned(claim.agent_run_id, claim.owner, claim.generation),
            "REVISION_CONFLICT",
            "Agent lease is no longer owned",
        )
        task = uow.tasks.get(claim.project_id, claim.task_id, lock=True)
        require_agent_task_active(task, run, claim.generation)
        return {"run": run, "task": task}

    def prepare_frame(self, claim: AgentLeaseClaim) -> DecisionFrame:
        with self.store.transaction() as uow:
            owned = self._require_owned(uow, claim)
            run, task = owned["run"], owned["task"]
            pending = uow.agents.pending_model_frame(run["agent_run_id"])
            if pending is not None:
                require(
                    pending["task_spec_revision"] == task.spec_revision
                    and pending["control_version"] == task.control_version,
                    "REVISION_CONFLICT",
                    "Pending model frame targets an old Task revision",
                )
                return DecisionFrame(
                    agent_run_id=pending["agent_run_id"],
                    frame_id=pending["frame_id"],
                    task_id=task.task_id,
                    task_spec_revision=pending["task_spec_revision"],
                    control_version=pending["control_version"],
                    generation=pending["generation"],
                    context_manifest_digest=pending["context_manifest_digest"],
                    payload=pending["payload"],
                )
            uow.agents.stale_pending_frames(run["agent_run_id"])
            plan = uow.agents.plan(run["agent_run_id"])
            events = uow.agents.events(run["agent_run_id"])
            answers = uow.agents.answers(run["agent_run_id"])
            wakeups = uow.agents.pending_wakeups(run["agent_run_id"], lock=True)
            task_spec = uow.tasks.spec(task.task_id, task.spec_revision)
            if self.context_compiler is not None:
                budget = uow.budgets.get(task.task_id)
                compiled = self.context_compiler.compile(
                    task={
                        "task_id": task.task_id,
                        "project_id": task.project_id,
                        "spec_revision": task.spec_revision,
                        "control_version": task.control_version,
                        "lifecycle": task.lifecycle,
                        "control": task.control,
                    },
                    task_spec=task_spec,
                    budget=asdict(budget),
                    plan=plan["plan"] if plan else None,
                    events=events,
                    answers=[value["answer"] for value in answers],
                    tool_observations=uow.agents.tool_observations(run["agent_run_id"]),
                    pending_wake_ids=[value["wake_id"] for value in wakeups],
                )
                manifest, payload = compiled.manifest, compiled.payload
            else:
                manifest = {
                    "schema_version": 1,
                    "task": {
                        "task_id": task.task_id,
                        "spec_revision": task.spec_revision,
                        "control_version": task.control_version,
                    },
                    "plan": (
                        {"revision": plan["revision"], "digest": plan["plan_digest"]}
                        if plan
                        else None
                    ),
                    "events": [
                        {
                            "event_id": value["event_id"],
                            "source": value["source"],
                            "source_revision": value["source_revision"],
                            "digest": value["event_digest"],
                        }
                        for value in events
                    ],
                    "answers": [
                        {
                            "answer_id": value["answer_id"],
                            "question_id": value["question_id"],
                            "digest": value["answer_digest"],
                        }
                        for value in answers
                    ],
                }
                payload = {
                    "task_spec": task_spec,
                    "plan": plan["plan"] if plan else None,
                    "events": [value["payload"] for value in events],
                    "answers": [value["answer"] for value in answers],
                    "pending_wake_ids": [value["wake_id"] for value in wakeups],
                }
            manifest_digest = canonical_digest(manifest)
            context = uow.agents.add_context(
                {
                    "manifest_id": uow.new_id(),
                    "agent_run_id": run["agent_run_id"],
                    "task_spec_revision": task.spec_revision,
                    "manifest": manifest,
                    "manifest_digest": manifest_digest,
                    "created_at": uow.now(),
                }
            )
            sequence = run["frame_sequence"] + 1
            frame = DecisionFrame(
                agent_run_id=run["agent_run_id"],
                frame_id=uow.new_id(),
                task_id=task.task_id,
                task_spec_revision=task.spec_revision,
                control_version=task.control_version,
                generation=claim.generation,
                context_manifest_digest=manifest_digest,
                payload=payload,
            )
            uow.agents.add_frame(
                {
                    "frame_id": frame.frame_id,
                    "agent_run_id": run["agent_run_id"],
                    "sequence": sequence,
                    "epoch": run["epoch"],
                    "generation": claim.generation,
                    "task_spec_revision": task.spec_revision,
                    "control_version": task.control_version,
                    "context_manifest_id": context["manifest_id"],
                    "context_manifest_digest": manifest_digest,
                    "payload": payload,
                    "frame_digest": frame.digest,
                    "state": "PENDING",
                    "created_at": uow.now(),
                }
            )
            uow.agents.update_run(
                run["agent_run_id"], frame_sequence=sequence, updated_at=uow.now()
            )
            return frame

    def accept_proposal(
        self,
        claim: AgentLeaseClaim,
        frame: DecisionFrame,
        proposal: DecisionProposal,
        *,
        model_call_id: str | None = None,
    ) -> Record:
        with self.store.transaction() as uow:
            saved_frame = uow.agents.frame(frame.frame_id, lock=True)
            existing = uow.agents.proposal(frame.frame_id)
            if existing is not None:
                require(
                    saved_frame["agent_run_id"] == claim.agent_run_id
                    and saved_frame["epoch"] == claim.epoch
                    and saved_frame["generation"] == claim.generation
                    and saved_frame["frame_digest"] == frame.digest
                    and existing["proposal_digest"] == proposal.digest,
                    "INVALID_BINDING",
                    "Frame identity has conflicting proposals",
                    422,
                )
                return existing
            owned = self._require_owned(uow, claim)
            run, task = owned["run"], owned["task"]
            reconciled_model = False
            if model_call_id is not None:
                call = uow.agents.model_call(model_call_id, lock=True)
                require(
                    call["agent_run_id"] == run["agent_run_id"]
                    and call["frame_id"] == frame.frame_id
                    and call["status"] == "COMPLETED"
                    and call["proposal_digest"] == proposal.digest,
                    "INVALID_BINDING",
                    "Model call does not prove this proposal",
                    422,
                )
                reconciled_model = True
            require(
                saved_frame["agent_run_id"] == run["agent_run_id"]
                and (saved_frame["epoch"] == run["epoch"] or reconciled_model)
                and (saved_frame["generation"] == claim.generation or reconciled_model)
                and saved_frame["state"] == "PENDING"
                and saved_frame["frame_digest"] == frame.digest,
                "REVISION_CONFLICT",
                "Decision frame is stale",
            )
            require(
                proposal.frame_id == frame.frame_id
                and proposal.expected_task_revision == task.spec_revision,
                "INVALID_BINDING",
                "Proposal does not match its decision frame",
                422,
            )
            allowed = saved_frame["payload"].get("allowed_proposal_kinds")
            if allowed is not None:
                require(
                    proposal.kind in allowed,
                    "UNSUPPORTED_TARGET",
                    "Proposal kind is not enabled for this Agent stage",
                    422,
                )
            now = uow.now()
            record = {
                "proposal_id": uow.new_id(),
                "agent_run_id": run["agent_run_id"],
                "frame_id": frame.frame_id,
                "epoch": run["epoch"],
                "generation": claim.generation,
                "kind": proposal.kind,
                "proposal": proposal.as_record(),
                "proposal_digest": proposal.digest,
                "accepted_at": now,
            }
            uow.agents.add_proposal(record)
            plan_revision = run["plan_revision"]
            wait_satisfied = False
            if proposal.kind == "PLAN_REVISION":
                plan_revision += 1
                uow.agents.add_plan(
                    {
                        "agent_run_id": run["agent_run_id"],
                        "revision": plan_revision,
                        "task_spec_revision": task.spec_revision,
                        "frame_id": frame.frame_id,
                        "generation": claim.generation,
                        "plan": dict(proposal.body),
                        "plan_digest": canonical_digest(proposal.body),
                        "created_at": now,
                    }
                )
            elif proposal.kind == "CLARIFICATION_REQUEST":
                gap_id = proposal.body.get("gap_id")
                require(valid_identifier(gap_id), "INVALID_BINDING", "Invalid question gap", 422)
                uow.agents.add_question(
                    {
                        "question_id": uow.new_id(),
                        "agent_run_id": run["agent_run_id"],
                        "frame_id": frame.frame_id,
                        "gap_id": gap_id,
                        "question": dict(proposal.body),
                        "status": "OPEN",
                        "created_at": now,
                    }
                )
            elif proposal.kind == "WAIT_REQUEST":
                wait_satisfied = persist_wait_dependencies(
                    uow,
                    run,
                    record,
                    list(proposal.body.get("dependencies", [])),
                )
            wake_ids = list(saved_frame["payload"].get("pending_wake_ids", []))
            uow.agents.consume_wakeups(wake_ids, now)
            uow.agents.update_frame(frame.frame_id, state="ACCEPTED")
            status, blocked_reason = outcome_state(proposal.kind)
            wake_revision = run["wake_revision"]
            if wait_satisfied:
                wake_revision += 1
                uow.agents.add_wakeup(
                    {
                        "wake_id": uow.new_id(),
                        "agent_run_id": run["agent_run_id"],
                        "revision": wake_revision,
                        "reason": "DEPENDENCY",
                        "event_ids": [record["proposal_id"]],
                        "state": "PENDING",
                        "created_at": now,
                        "consumed_at": None,
                    }
                )
                status = "READY"
            uow.agents.update_run(
                run["agent_run_id"],
                status=status,
                plan_revision=plan_revision,
                accepted_proposals=run["accepted_proposals"] + 1,
                wake_revision=wake_revision,
                blocked_reason=blocked_reason,
                updated_at=now,
            )
            uow.agents.release_lease(run["agent_run_id"], claim.owner, claim.generation)
            uow.agents.add_outbox(
                {
                    "event_id": uow.new_id(),
                    "agent_run_id": run["agent_run_id"],
                    "kind": "PROPOSAL_ACCEPTED",
                    "payload": {
                        "proposal_id": record["proposal_id"],
                        "frame_id": frame.frame_id,
                        "kind": proposal.kind,
                    },
                    "done": False,
                    "available_at": now,
                    "owner": None,
                    "lease_until": None,
                    "generation": 0,
                }
            )
            return record

    def register_event(
        self,
        project_id: str,
        task_id: str,
        *,
        source: str,
        event_id: str,
        stream_id: str,
        source_revision: int,
        kind: str,
        payload: Record,
    ) -> bool:
        require(
            valid_identifier(event_id) and valid_identifier(stream_id),
            "INVALID_BINDING",
            "Invalid Agent event identity",
            422,
        )
        require(
            type(source_revision) is int and source_revision > 0,
            "INVALID_BINDING",
            "Invalid Agent event revision",
            422,
        )
        frozen = {
            "source": source,
            "event_id": event_id,
            "stream_id": stream_id,
            "source_revision": source_revision,
            "kind": kind,
            "payload": payload,
        }
        event_digest = canonical_digest(frozen)
        with self.store.transaction() as uow:
            task = uow.tasks.get(project_id, task_id, lock=True)
            run = uow.agents.run_by_task(task_id, lock=True, required=True)
            now = uow.now()
            accepted = uow.agents.register_event(
                frozen
                | {
                    "agent_run_id": run["agent_run_id"],
                    "event_digest": event_digest,
                    "observed_at": now,
                }
            )
            if not accepted:
                return False
            uow.agents.update_watermark(
                {
                    "agent_run_id": run["agent_run_id"],
                    "source": source,
                    "stream_id": stream_id,
                    "latest_revision": source_revision,
                    "latest_event_digest": event_digest,
                    "updated_at": now,
                }
            )
            dependencies = uow.agents.wait_dependencies(
                run["agent_run_id"], active_only=True, lock=True
            )
            for dependency in dependencies:
                if dependency["owner"] != source or dependency["object_id"] != stream_id:
                    continue
                fact = dependency_fact(uow, run, dependency)
                state = (
                    "SATISFIED"
                    if fact["satisfied"]
                    else ("UNKNOWN" if fact["unknown"] else "WAITING")
                )
                uow.agents.update_wait_dependency(
                    dependency["dependency_id"],
                    observed_revision=fact["revision"],
                    state=state,
                    state_digest=canonical_digest(fact),
                    updated_at=now,
                )
            remaining = uow.agents.wait_dependencies(run["agent_run_id"], active_only=True)
            if not dependencies or not remaining:
                if task.lifecycle != "OPEN":
                    uow.agents.update_run(run["agent_run_id"], status="STOPPED", updated_at=now)
                elif task.control == "ACTIVE":
                    wake_run(uow, run, [event_id], source)
            return True

    def answer_question(
        self,
        actor: Identity,
        project_id: str,
        agent_run_id: str,
        question_id: str,
        key: str,
        expected_spec_revision: int,
        answer: Record,
    ) -> Record:
        require(0 < len(key) <= 128 and key.isascii(), "MALFORMED_REQUEST", "Invalid key", 400)
        payload = {
            "agent_run_id": agent_run_id,
            "question_id": question_id,
            "expected_spec_revision": expected_spec_revision,
            "answer": answer,
        }
        with self.store.transaction() as uow:
            uow.access.authorize(actor, project_id, "EDITOR")
            replay = uow.delivery.idempotency(
                actor, project_id, "answer_agent_question", key, digest(payload)
            )
            if replay is not None:
                return replay
            run = uow.agents.run(project_id, agent_run_id, lock=True)
            task = uow.tasks.get(project_id, run["task_id"], lock=True)
            task.admissible(expected_spec_revision)
            require(
                run["task_spec_revision"] == task.spec_revision,
                "REVISION_CONFLICT",
                "AgentRun is based on another Task revision",
            )
            question = uow.agents.question(agent_run_id, question_id, lock=True)
            require(question["status"] == "OPEN", "PRECONDITION_UNRESOLVED", "Question is closed")
            now = uow.now()
            database_record = {
                "answer_id": uow.new_id(),
                "question_id": question_id,
                "agent_run_id": agent_run_id,
                "task_spec_revision": task.spec_revision,
                "answer": answer,
                "answer_digest": canonical_digest(answer),
                "answered_by": actor.principal_id,
                "answered_at": now,
            }
            uow.agents.add_answer(database_record)
            revision = run["wake_revision"] + 1
            uow.agents.add_wakeup(
                {
                    "wake_id": uow.new_id(),
                    "agent_run_id": agent_run_id,
                    "revision": revision,
                    "reason": "USER",
                    "event_ids": [database_record["answer_id"]],
                    "state": "PENDING",
                    "created_at": now,
                    "consumed_at": None,
                }
            )
            uow.agents.update_run(
                agent_run_id,
                status="READY",
                generation=run["generation"] + 1,
                wake_revision=revision,
                updated_at=now,
            )
            receipt = database_record | {"answered_at": now.isoformat()}
            uow.delivery.remember(actor, project_id, "answer_agent_question", key, receipt)
            return receipt

    def recover_runtime_from_ledger(self, claim: AgentLeaseClaim, reason: str) -> Record:
        require(
            reason in {"MISSING", "CORRUPT", "BEHIND", "AHEAD"},
            "MALFORMED_REQUEST",
            "Unknown runtime recovery reason",
            400,
        )
        with self.store.transaction() as uow:
            owned = self._require_owned(uow, claim)
            run = owned["run"]
            epoch = run["epoch"] + 1
            generation = run["generation"] + 1
            ledger = {
                "agent_run_id": run["agent_run_id"],
                "from_epoch": run["epoch"],
                "frame_sequence": run["frame_sequence"],
                "accepted_proposals": run["accepted_proposals"],
                "wake_revision": run["wake_revision"],
                "task_spec_revision": run["task_spec_revision"],
                "control_version": run["control_version"],
            }
            recovery_id = uow.new_id()
            uow.agents.add_runtime_recovery(
                {
                    "recovery_id": recovery_id,
                    "agent_run_id": run["agent_run_id"],
                    "from_epoch": run["epoch"],
                    "to_epoch": epoch,
                    "reason": reason,
                    "ledger_revision": run["accepted_proposals"],
                    "ledger_digest": canonical_digest(ledger),
                    "created_at": uow.now(),
                }
            )
            if uow.agents.pending_model_frame(run["agent_run_id"]) is None:
                uow.agents.stale_pending_frames(run["agent_run_id"])
            uow.agents.revoke_lease(run["agent_run_id"])
            uow.agents.update_run(
                run["agent_run_id"],
                status="READY",
                epoch=epoch,
                generation=generation,
                runtime_ref=f"{run['agent_run_id']}-e{epoch}",
                blocked_reason=f"RECOVERED_FROM_LEDGER_{reason}",
                updated_at=uow.now(),
            )
            return {
                "agent_run_id": run["agent_run_id"],
                "epoch": epoch,
                "generation": generation,
                "runtime_ref": f"{run['agent_run_id']}-e{epoch}",
                "reason": reason,
                "recovery_id": recovery_id,
            }

    def request_checkpoint_rebuild(self, agent_run_id: str) -> Record:
        """Rotate an idle runtime identity so the next worker rebuilds from the ledger.

        This administrative operation never changes the scientific/task state and
        refuses to race an active Agent worker.  WAITING and QUESTION runs keep
        their state, so rebuilding a framework checkpoint cannot create a model
        turn that was not justified by a source event or user answer.
        """
        require(
            valid_identifier(agent_run_id),
            "INVALID_BINDING",
            "Invalid Agent run identity",
            422,
        )
        with self.store.transaction() as uow:
            run = uow.agents.run_by_id(agent_run_id, lock=True)
            require(
                run["status"] in {"READY", "WAITING", "QUESTION"},
                "REVISION_CONFLICT",
                "Checkpoint rebuild requires an idle resumable Agent run",
            )
            require(
                uow.agents.active_lease(agent_run_id) is None,
                "REVISION_CONFLICT",
                "Checkpoint rebuild cannot race an active Agent worker",
            )
            epoch = run["epoch"] + 1
            generation = run["generation"] + 1
            ledger = {
                "agent_run_id": run["agent_run_id"],
                "from_epoch": run["epoch"],
                "frame_sequence": run["frame_sequence"],
                "accepted_proposals": run["accepted_proposals"],
                "wake_revision": run["wake_revision"],
                "task_spec_revision": run["task_spec_revision"],
                "control_version": run["control_version"],
            }
            recovery_id = uow.new_id()
            uow.agents.add_runtime_recovery(
                {
                    "recovery_id": recovery_id,
                    "agent_run_id": agent_run_id,
                    "from_epoch": run["epoch"],
                    "to_epoch": epoch,
                    "reason": "ADMIN_REQUEST",
                    "ledger_revision": run["accepted_proposals"],
                    "ledger_digest": canonical_digest(ledger),
                    "created_at": uow.now(),
                }
            )
            uow.agents.update_run(
                agent_run_id,
                epoch=epoch,
                generation=generation,
                runtime_ref=f"{agent_run_id}-e{epoch}",
                blocked_reason="RECOVERED_FROM_LEDGER_ADMIN_REQUEST",
                updated_at=uow.now(),
            )
            return {
                "agent_run_id": agent_run_id,
                "epoch": epoch,
                "generation": generation,
                "runtime_ref": f"{agent_run_id}-e{epoch}",
                "reason": "ADMIN_REQUEST",
                "recovery_id": recovery_id,
                "status": run["status"],
            }

    def release_transient(self, claim: AgentLeaseClaim) -> None:
        with self.store.transaction() as uow:
            run = uow.agents.run(claim.project_id, claim.agent_run_id, lock=True)
            if not uow.agents.lease_owned(claim.agent_run_id, claim.owner, claim.generation):
                return
            uow.agents.release_lease(claim.agent_run_id, claim.owner, claim.generation)
            uow.agents.update_run(run["agent_run_id"], status="READY", updated_at=uow.now())

    def runtime_revision(self, claim: AgentLeaseClaim) -> int:
        with self.store.transaction() as uow:
            self._require_owned(uow, claim)
            return sum(
                value["epoch"] == claim.epoch for value in uow.agents.proposals(claim.agent_run_id)
            )

    def view(self, actor: Identity, project_id: str, agent_run_id: str) -> Record:
        with self.store.transaction() as uow:
            uow.access.authorize(actor, project_id)
            run = uow.agents.run(project_id, agent_run_id)
            return {
                **run,
                "plan": uow.agents.plan(agent_run_id),
                "frames": uow.agents.frames(agent_run_id),
                "proposals": uow.agents.proposals(agent_run_id),
                "questions": uow.agents.questions(agent_run_id),
                "wakeups": uow.agents.wakeups(agent_run_id),
                "events": uow.agents.events(agent_run_id),
                "wait_dependencies": uow.agents.wait_dependencies(agent_run_id),
                "runtime_recoveries": uow.agents.runtime_recoveries(agent_run_id),
            }

    def task_view(self, actor: Identity, project_id: str, task_id: str) -> Record:
        """Return a user-visible projection without model context or checkpoints."""
        with self.store.transaction() as uow:
            uow.access.authorize(actor, project_id)
            task = uow.tasks.get(project_id, task_id)
            run = uow.agents.run_by_task(task_id, required=True)
            plan = uow.agents.plan(run["agent_run_id"])
            questions = uow.agents.questions(run["agent_run_id"])
            proposals = uow.agents.proposals(run["agent_run_id"])[-20:]
            observations = uow.agents.tool_observations(run["agent_run_id"])[-50:]
            action_links = uow.agents.action_links(run["agent_run_id"])[-50:]
            wait_dependencies = uow.agents.wait_dependencies(run["agent_run_id"])[-64:]
            recoveries = uow.agents.runtime_recoveries(run["agent_run_id"])[-20:]
            delivery_attempts = uow.agents.delivery_attempts(run["agent_run_id"])[-50:]
            usage = (
                uow.agents.model_usage(run["agent_run_id"])
                if uow.agents.model_budget(run["agent_run_id"], required=False)
                else None
            )
            return {
                "agent_run_id": run["agent_run_id"],
                "task_ref": {"task_id": task.task_id, "spec_revision": task.spec_revision},
                "status": run["status"],
                "epoch": run["epoch"],
                "generation": run["generation"],
                "wake_revision": run["wake_revision"],
                "runtime_profile_id": run["runtime_profile"]["profile_id"],
                "runtime_profile_digest": run["runtime_profile_digest"],
                "plan_revision": run["plan_revision"],
                "plan": plan["plan"] if plan else None,
                "blocked_reason": run["blocked_reason"],
                "questions": [
                    {
                        "question_id": value["question_id"],
                        "gap_id": value["gap_id"],
                        "question": value["question"],
                        "status": value["status"],
                        "created_at": value["created_at"].isoformat(),
                    }
                    for value in questions
                ],
                "recent_decisions": [
                    {
                        "proposal_id": value["proposal_id"],
                        "frame_id": value["frame_id"],
                        "kind": value["kind"],
                        "message": value["proposal"].get("body", {}).get("message"),
                        "accepted_at": value["accepted_at"].isoformat(),
                    }
                    for value in proposals
                ],
                "read_observations": [
                    {
                        "observation_id": value["observation_id"],
                        "tool_request_id": value["tool_request_id"],
                        "tool_id": value["tool_id"],
                        "status": value["status"],
                        "result_digest": value["result_digest"],
                        "refs": value["refs"],
                        "error_category": value["error_category"],
                        "observed_at": value["observed_at"].isoformat(),
                    }
                    for value in observations
                ],
                "action_links": [
                    {
                        "link_id": value["link_id"],
                        "tool_request_id": value["tool_request_id"],
                        "operation": value["operation"],
                        "business_id": value["business_id"],
                        "action_id": value["action_id"],
                        "command_id": value["command_id"],
                        "created_at": value["created_at"].isoformat(),
                    }
                    for value in action_links
                ],
                "wait_dependencies": [
                    {
                        "dependency_id": value["dependency_id"],
                        "owner": value["owner"],
                        "object_id": value["object_id"],
                        "minimum_revision": value["minimum_revision"],
                        "observed_revision": value["observed_revision"],
                        "state": value["state"],
                    }
                    for value in wait_dependencies
                ],
                "runtime_recoveries": [
                    {
                        "recovery_id": value["recovery_id"],
                        "from_epoch": value["from_epoch"],
                        "to_epoch": value["to_epoch"],
                        "reason": value["reason"],
                        "ledger_digest": value["ledger_digest"],
                        "created_at": value["created_at"].isoformat(),
                    }
                    for value in recoveries
                ],
                "delivery_attempts": [
                    {
                        "attempt_id": value["attempt_id"],
                        "tool_id": value["tool_id"],
                        "ordinal": value["ordinal"],
                        "outcome": value["outcome"],
                        "failure_category": value["failure_category"],
                        "retry_of_attempt_id": value["retry_of_attempt_id"],
                        "stop_reason": value["stop_reason"],
                    }
                    for value in delivery_attempts
                ],
                "model_usage": usage,
            }
