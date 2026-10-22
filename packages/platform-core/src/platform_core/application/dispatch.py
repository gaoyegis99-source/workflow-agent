"""Leased action delivery and reconciliation; external calls occur in the worker adapter."""

from dataclasses import asdict, replace

from platform_core.application.control import digest
from platform_core.application.ports import Record, Store, UnitOfWork
from platform_core.domain.control import ExternalObservation, Identity, RuleViolation, Task, require


class Dispatcher:
    def __init__(self, store: Store):
        self.store = store

    def claim(self, owner: str, lease_seconds: float = 30) -> Record | None:
        with self.store.transaction() as uow:
            return uow.delivery.claim(owner, lease_seconds)

    def renew(self, claim: Record, lease_seconds: float = 30) -> bool:
        with self.store.transaction() as uow:
            return uow.delivery.renew(
                claim["event_id"], claim["owner"], claim["generation"], lease_seconds
            )

    @staticmethod
    def _owned(uow: UnitOfWork, claim: Record) -> bool:
        return uow.delivery.owned(claim["event_id"], claim["owner"], claim["generation"])

    @staticmethod
    def _finish(uow: UnitOfWork, claim: Record, delay: float | None = None) -> None:
        uow.delivery.finish(claim["event_id"], claim["owner"], claim["generation"], delay)

    @staticmethod
    def _cleanup(uow: UnitOfWork, task: Task) -> None:
        if task.lifecycle != "CANCELLED":
            return
        active = uow.actions.active(task.task_id)
        state = (
            "CLEAR"
            if not active
            else "UNKNOWN"
            if any(a["state"] == "RECONCILING" for a in active)
            else "PENDING"
        )
        if task.cleanup_status != state:
            uow.tasks.save(replace(task, cleanup_status=state))

    def _reject(
        self, uow: UnitOfWork, claim: Record, task: Task, action: Record, code: str
    ) -> None:
        envelope = uow.workflows.envelope(action["action_id"], required=False)
        if envelope is not None:
            remaining = uow.workflows.release_envelope(action["action_id"])
            budget = uow.budgets.get(task.task_id)
            require(
                budget.reserved >= remaining,
                "INVALID_BINDING",
                "Workflow reservation is inconsistent",
            )
            uow.budgets.save(replace(budget, reserved=budget.reserved - remaining))
            uow.actions.update(action["action_id"], state="REJECTED", stopped=True)
            uow.actions.update_command(
                action["command_id"],
                status="REJECTED",
                error_code=code,
                error_message="Action no longer satisfies task admission requirements",
            )
            self._cleanup(uow, task)
            self._finish(uow, claim)
            return
        reservation = uow.budgets.reservation(action["action_id"])
        require(
            not action["send_started"] and not reservation["charged"],
            "PRECONDITION_UNRESOLVED",
            "Cannot release an uncertain submission",
        )
        uow.budgets.save(
            uow.budgets.get(task.task_id).account(was_charged=False, charged=False, stopped=True)
        )
        uow.budgets.update_reservation(
            action["action_id"],
            source="PLATFORM",
            event_id=claim["event_id"],
            charged=False,
            stopped=True,
        )
        uow.actions.update(action["action_id"], state="REJECTED", stopped=True)
        uow.actions.update_command(
            action["command_id"],
            status="REJECTED",
            error_code=code,
            error_message="Action no longer satisfies task admission requirements",
        )
        self._cleanup(uow, task)
        self._finish(uow, claim)

    def prepare(
        self, claim: Record, *, execution_available: bool = True, absent: bool = False
    ) -> Record | None:
        with self.store.transaction() as uow:
            project = claim["payload"]["project_id"]
            actor_error = None
            if claim["kind"] == "ACTION":
                preliminary = uow.actions.get(claim["payload"]["action_id"])
                if not preliminary["send_started"] or absent:
                    # Match API lock order: identity/member, then task, then delivery.
                    # Taking membership locks after the task could deadlock with
                    # a concurrent membership change and an API command.
                    try:
                        if preliminary["token_id"] is None:
                            uow.access.authorize_principal(
                                preliminary["principal_id"], project, "EDITOR"
                            )
                        else:
                            uow.access.authorize(
                                Identity(preliminary["principal_id"], preliminary["token_id"]),
                                project,
                                "EDITOR",
                            )
                    except RuleViolation as error:
                        actor_error = error.code
            task = uow.tasks.get(project, claim["task_id"], lock=True)
            if not self._owned(uow, claim):
                return None
            if claim["kind"] == "TASK_EVENT":
                if uow.delivery.inbox("PLATFORM", claim["event_id"], digest(claim["payload"])):
                    uow.delivery.audit(claim["event_id"], task.task_id, claim["payload"])
                self._finish(uow, claim)
                return None
            action = uow.actions.get(claim["payload"]["action_id"])
            if action["stopped"]:
                self._finish(uow, claim)
                return None
            request = action["request"]
            cancelled = action["cancel_requested"] or task.lifecycle == "CANCELLED"
            permitted = uow.tasks.permits(
                task.task_id, request["target"], request["resources"], uow.now()
            )
            if request.get("kind") == "WORKFLOW":
                permitted = permitted and all(
                    uow.tasks.permits(task.task_id, target, request["resources"], uow.now())
                    for target in request["leaf_targets"]
                )
            invalid = None
            if not permitted or task.grant_epoch != action["grant_epoch"]:
                invalid = "SCOPE_DENIED"
            if task.spec_revision != action["spec_revision"]:
                invalid = "REVISION_CONFLICT"
            if actor_error:
                invalid = actor_error
            if not action["send_started"]:
                if cancelled or invalid:
                    self._reject(uow, claim, task, action, invalid or "PRECONDITION_UNRESOLVED")
                    return None
                if task.control == "PAUSED" or not execution_available:
                    self._finish(uow, claim, 1)
                    return None
                # Durable send intent precedes the external call. After this point,
                # ABSENT/timeout can never justify releasing a reservation.
                uow.actions.update(action["action_id"], send_started=True, state="DISPATCHING")
                uow.actions.update_command(action["command_id"], status="DISPATCHING")
                method = "submit"
            elif cancelled or not permitted or (absent and invalid):
                uow.actions.update(action["action_id"], cancel_requested=True)
                method = "cancel"
            elif absent:
                if task.control == "PAUSED" or action["execution_id"] is not None:
                    self._uncertain(uow, claim, task, action)
                    return None
                method = "submit"
            else:
                method = "query"
            if not execution_available:
                self._finish(uow, claim, 1)
                return None
            return {"method": method, "action_id": action["action_id"], "request": request}

    def observe(self, claim: Record, observation: ExternalObservation) -> bool:
        observation.validate()
        with self.store.transaction() as uow:
            task = uow.tasks.get(claim["payload"]["project_id"], claim["task_id"], lock=True)
            if not self._owned(uow, claim):
                return False
            action = uow.actions.get(claim["payload"]["action_id"])
            require(
                observation.action_id == action["action_id"],
                "INVALID_BINDING",
                "Observation belongs to another action",
                422,
            )
            # Validate event identity even for old revisions; reusing an event ID
            # with different contents is never a valid reconciliation response.
            uow.delivery.inbox("EXECUTION", observation.event_id, digest(asdict(observation)))
            fact = asdict(observation)
            del fact["event_id"]
            if observation.revision >= action["external_revision"]:
                require(
                    action["execution_id"] in {None, observation.execution_id},
                    "INVALID_BINDING",
                    "Execution identity changed",
                    422,
                )
                envelope = uow.workflows.envelope(action["action_id"], required=False)
                reservation = (
                    None if envelope is not None else uow.budgets.reservation(action["action_id"])
                )
                same_revision = observation.revision == action["external_revision"]
                if same_revision:
                    snapshot = action["last_external_observation"]
                    if snapshot is not None:
                        consistent = snapshot == fact
                    elif envelope is not None:
                        consistent = (
                            action["execution_id"] == observation.execution_id
                            and action["stopped"] == observation.stopped
                            and action["state"] in {observation.state, "RECONCILING"}
                        )
                    else:
                        # An upgraded 0001 row has no snapshot. Only an observation
                        # consistent with all surviving facts can fill the gap.
                        consistent = (
                            action["execution_id"] == observation.execution_id
                            and reservation["charged"] == observation.executed
                            and reservation["stopped"] == observation.stopped
                            and action["stopped"] == observation.stopped
                            and action["state"] in {observation.state, "RECONCILING"}
                        )
                    require(consistent, "INVALID_BINDING", "Conflicting observation facts", 422)
                else:
                    require(
                        not action["stopped"]
                        and (reservation is None or not reservation["stopped"]),
                        "INVALID_BINDING",
                        "Terminal execution facts cannot change",
                        422,
                    )
                    if envelope is None:
                        require(
                            not reservation["charged"] or observation.executed,
                            "INVALID_BINDING",
                            "Confirmed execution cannot be retracted",
                            422,
                        )
                        uow.budgets.save(
                            uow.budgets.get(task.task_id).account(
                                was_charged=reservation["charged"],
                                charged=observation.executed,
                                stopped=observation.stopped,
                            )
                        )
                        uow.budgets.update_reservation(
                            action["action_id"],
                            source="EXECUTION",
                            event_id=observation.event_id,
                            charged=observation.executed,
                            stopped=observation.stopped,
                        )
                    elif observation.stopped:
                        remaining = uow.workflows.release_envelope(action["action_id"])
                        budget = uow.budgets.get(task.task_id)
                        require(
                            budget.reserved >= remaining,
                            "INVALID_BINDING",
                            "Workflow reservation is inconsistent",
                        )
                        uow.budgets.save(replace(budget, reserved=budget.reserved - remaining))
                # Equal facts restore communication health without re-settling.
                uow.actions.update(
                    action["action_id"],
                    state=observation.state,
                    external_revision=observation.revision,
                    execution_id=observation.execution_id,
                    stopped=observation.stopped,
                    failure_count=0,
                    last_external_observation=fact,
                )
                if observation.execution_id is not None:
                    uow.actions.update_command(
                        action["command_id"],
                        status="RESOLVED",
                        execution_id=observation.execution_id,
                    )
                else:
                    uow.actions.update_command(
                        action["command_id"],
                        status="REJECTED",
                        error_code="PRECONDITION_UNRESOLVED",
                        error_message="External request sealed or rejected without execution",
                    )
            # Older observations neither clear unknown state nor reset failures.
            current = uow.actions.get(action["action_id"])
            self._cleanup(uow, task)
            self._finish(uow, claim, None if current["stopped"] else 1)
            return True

    def _uncertain(self, uow: UnitOfWork, claim: Record, task: Task, action: Record) -> None:
        failures = min(6, action["failure_count"] + 1)
        uow.actions.update(action["action_id"], state="RECONCILING", failure_count=failures)
        if action["execution_id"] is None:
            uow.actions.update_command(action["command_id"], status="RECONCILING")
        self._cleanup(uow, task)
        self._finish(uow, claim, min(30, 2 ** (failures - 1)))

    def uncertain(self, claim: Record) -> bool:
        with self.store.transaction() as uow:
            task = uow.tasks.get(claim["payload"]["project_id"], claim["task_id"], lock=True)
            if not self._owned(uow, claim):
                return False
            action = uow.actions.get(claim["payload"]["action_id"])
            self._uncertain(uow, claim, task, action)
            return True
