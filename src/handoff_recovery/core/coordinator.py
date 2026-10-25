"""Bounded recovery preparation followed by local-only execution.

This is an independent prototype protocol, not itself a run_task agent.
Each step requests at most one local decision. GUI actions require an explicit
host acknowledgement; scoped verification completion is returned to the host
without another local decision hidden inside that call. The host owns device
execution and scoring. The public Benchmark bridge preserves this accounting.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import time

from .types import (
    HANDOFFBENCH_ACTIONS, Handoff, LocalInstruction, OwnAction, PlanReply, PlanRequest,
    PublicFrame, ProgressSummary, sanitize_progress,
    GUI_BUDGET_SEMANTICS, RecoveryBudget, SlimPlan, StepResult, VerificationTask,
    validate_completion_check, validate_condition_updates, RECOVERY_CONDITION_STATUSES,
)
from .protocols import (ExecutorNoOp, ExecutorProtocol, PlannerProtocol,
                        error_metadata, classify_error, history_policy_descriptor,
                        validate_executor)


class LifecycleError(ValueError):
    """A host call violates the explicit observation/control protocol."""

    category = "lifecycle_error"


CONDITION_UPDATE_POLICIES = frozenset({"host", "model_claim"})


class RecoveryCoordinator:
    # Keep the recovery contract aligned with HandoffBench's complete public
    # action vocabulary. The host still performs full field/value validation.
    _ACTIONS = HANDOFFBENCH_ACTIONS
    # Waiting is an intentional observation action and may legitimately repeat
    # while an asynchronous device operation settles. Navigation and mutation
    # actions are guarded when they repeat without a visible state change.
    _REPEAT_GUARD_ACTIONS = frozenset(HANDOFFBENCH_ACTIONS - {"wait", "done", "fail"})

    def __init__(self, *, task_id, original_goal, planner, executor, public_constraints=(),
                 budget=None, condition_update_policy="model_claim", completion_policy="conditions",
                 condition_updates_enabled=None):
        # Validate the public goal/constraints before starting a task.
        LocalInstruction(original_goal, tuple(public_constraints), "original")
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("task_id must be nonempty text")
        self.task_id = task_id
        self.original_goal = original_goal
        self.public_constraints = tuple(public_constraints)
        self.planner: PlannerProtocol = planner
        self.executor: ExecutorProtocol = validate_executor(executor)
        self.budget = budget if budget is not None else RecoveryBudget()
        if not isinstance(self.budget, RecoveryBudget):
            raise TypeError("budget must be a RecoveryBudget")
        if condition_update_policy not in CONDITION_UPDATE_POLICIES:
            raise ValueError("condition_update_policy must be host or model_claim")
        self.condition_update_policy = condition_update_policy
        if condition_updates_enabled is not None and type(condition_updates_enabled) is not bool:
            raise TypeError("condition_updates_enabled must be boolean or None")
        self.condition_updates_enabled = condition_updates_enabled
        if completion_policy not in {"conditions", "native"}:
            raise ValueError("completion_policy must be conditions or native")
        if completion_policy == "native" and getattr(self.executor, "requires_completion_check", False):
            raise ValueError("native completion cannot require completion_check")
        self.completion_policy = completion_policy
        self.phase = "original"
        self.decisions_used = 0
        self.cloud_calls = 0
        self.verification_rounds_used = 0
        self.terminal_reason = None
        self._history = []
        self._frames = {}
        self._decision_frames = set()
        self._last_frame = None
        self._pre_frame = None
        self._return_frame = None
        self._verification_frames = []
        self._verification = None
        self._verification_report = None
        self._verification_failed = False
        self._planner_completion_status = "INCOMPLETE"
        self._final_confirmation_pending = False
        self._handoff = None
        self._progress = ProgressSummary()
        self._awaiting_action = False
        self._events = []
        self._cloud_records = []
        self._execution_cloud_calls = None
        self._fallback_reason = None
        self._condition_status = ()
        # Validated claims on ordinary actions are held until the host
        # acknowledges the corresponding device action. Terminal ``done``
        # claims are applied atomically during ``step`` because there is no
        # device action acknowledgement for a terminal response.
        self._pending_condition_updates = ()
        self._last_action_signature = None
        self._last_action_frame_digest = None
        self._repeat_action_count = 0
        self._last_action_feedback = None
        self.executor.start_task(task_id, original_goal)
        self._emit("task_started", budget=self.budget.total_decisions)

    def _history_policy_descriptor(self):
        """Record the Executor's effective host history mode when available."""
        try:
            descriptor = getattr(self.executor, "descriptor", None)
        except Exception:
            # Metadata generation must never turn a completed task into a
            # protocol failure when an adapter cannot expose its descriptor.
            descriptor = None
        if isinstance(descriptor, dict) and isinstance(descriptor.get("history_policy"), dict):
            return deepcopy(descriptor["history_policy"])
        return history_policy_descriptor()

    @property
    def remaining_decisions(self):
        return self.budget.total_decisions - self.decisions_used

    @property
    def finished(self):
        return self.phase == "finished"

    @property
    def history(self):
        return tuple(deepcopy(self._history))

    def _emit(self, event, **payload):
        self._events.append({"event": event, "phase": self.phase,
                             "decisions_used": self.decisions_used, **deepcopy(payload)})

    def _observe(self, frame):
        if not isinstance(frame, PublicFrame):
            raise TypeError("Only a PublicFrame is accepted; no private runner objects")
        known = self._frames.get(frame.evidence_id)
        if known is not None and known != frame:
            raise LifecycleError("An evidence ID cannot be reassigned")
        self._frames[frame.evidence_id] = frame
        self._last_frame = frame
        if self._history and self._history[-1].state_changed is None:
            previous = self._history[-1]
            changed = self._frame_digest(frame) != self._frame_digest_by_id(previous.frame_id)
            self._history[-1] = replace(previous, state_changed=changed)
            self._last_action_feedback = {
                "action": previous.action.get("action"),
                "target": previous.action.get("target", ""),
                "result": previous.result,
                "state_changed": changed,
            }

    @staticmethod
    def _frame_digest(frame):
        return hashlib.sha256(frame.png).hexdigest()

    def _frame_digest_by_id(self, evidence_id):
        frame = self._frames.get(evidence_id)
        return self._frame_digest(frame) if frame is not None else None

    @staticmethod
    def _action_signature(action):
        if not isinstance(action, dict):
            return None
        return (action.get("action"), action.get("target"),
                tuple(action.get("coords", ())) if isinstance(action.get("coords"), list) else None,
                action.get("input_text"))

    def _initialize_condition_status(self, conditions):
        self._condition_status = tuple((condition, "pending") for condition in conditions)

    def update_condition_status(self, updates):
        """Apply monotonic host evidence to recovery completion conditions.

        A satisfied condition cannot be reopened. This hook is optional for
        hosts that can derive semantic state from UI accessibility data or a
        benchmark oracle; ordinary action acknowledgement still works without it.
        """
        if not isinstance(updates, (dict, list, tuple)):
            raise TypeError("condition updates must be a mapping or sequence")
        values = dict(updates) if isinstance(updates, dict) else dict(updates)
        current = dict(self._condition_status)
        for condition, status in values.items():
            if condition not in current or status not in RECOVERY_CONDITION_STATUSES:
                raise ValueError("Unknown recovery condition update")
            if current[condition] == "satisfied" and status != "satisfied":
                raise ValueError("Satisfied recovery conditions cannot regress")
            current[condition] = status
        self._condition_status = tuple((condition, current[condition])
                                       for condition, _ in self._condition_status)

    def pause_for_handoff(self, *, reopen_original_completion=False):
        # A host may defer an original-scope done while checking whether a
        # user takeover is due. Reopening is explicit and preserves all prior
        # decisions/history; it cannot reopen a failure or recovery completion.
        if type(reopen_original_completion) is not bool:
            raise TypeError("Completion reopening must be explicit boolean")
        if reopen_original_completion:
            if (not self.finished or self.terminal_reason != "task_complete"
                    or not self._history or self._history[-1].scope != "original"
                    or self.remaining_decisions <= 0):
                raise LifecycleError("Only a deferred original completion can enter a handoff")
            self.phase = "original"
            self.terminal_reason = None
            self._emit("original_completion_deferred_for_handoff")
        if self.phase != "original" or self._awaiting_action:
            raise LifecycleError("Pause requires original execution with no pending device action")
        self._pre_frame = self._last_frame
        self.phase = "paused"
        self._emit("handoff_started")

    def return_control(self, frame):
        if self.phase != "paused":
            raise LifecycleError("Control can return only after an explicit pause")
        if not isinstance(frame, PublicFrame) or frame.evidence_id in self._frames:
            raise LifecycleError("A fresh public post-return observation ID is required")
        self._observe(frame)
        self._return_frame = frame
        self.phase = "preparing"
        self._emit("control_returned", evidence_id=frame.evidence_id)

    def _finish(self, reason, *, action=None, error_type=None, error_category=None,
                error_class=None):
        if error_category is None and reason == "device_error":
            error_category = "device_error"
        if error_class is None and error_category is not None:
            error_class = classify_error(error_category, source="device"
                                         if error_category == "device_error" else "host")
        scope = self._local_scope()
        self.terminal_reason = reason
        self.phase = "finished"
        self._awaiting_action = False
        self._emit("task_terminated", reason=reason, error_type=error_type,
                   **({"error_category": error_category} if error_category else {}),
                   **({"error_class": error_class} if error_class else {}))
        return StepResult(reason, scope, self.decisions_used or None, deepcopy(action), error_type,
                          error_category, error_class)

    def _reject_pending_done(self, scope, frame, *, reason, pending=()):
        """Reject a premature recovery ``done`` without judging device state.

        This is an internal protocol guard: the host only checks that the
        model's terminal signal agrees with the condition state it previously
        declared. The next model call receives the rejection as feedback and
        can continue the recovery objective.
        """
        self._pending_condition_updates = ()
        self._awaiting_action = False
        self._history.append(OwnAction(
            self.decisions_used,
            {"action": "invalid_decision", "reason": reason},
            scope, frame.evidence_id, result="rejected", state_changed=False))
        self._last_action_feedback = {
            "action": "done",
            "target": "",
            "result": "rejected",
            "state_changed": False,
            "reason": reason,
            **({"pending_conditions": list(pending)} if pending else {}),
        }
        self._emit("done_rejected", reason=reason,
                   **({"pending_conditions": list(pending)} if pending else {}))
        return StepResult("invalid_decision", scope, self.decisions_used, None)

    def _local_scope(self):
        return {"verifying": "verification", "reviewing": "verification", "executing": "execution",
                "fallback_execution": "fallback"}.get(self.phase, "original")

    def _validate_completion_action(self, action, scope):
        """Gate recovery completion without judging visual evidence itself."""
        if (scope != "execution" or action.get("action") != "done"
                or not getattr(self.executor, "requires_completion_check", False)):
            return
        if self._handoff is None:
            raise ValueError("Completion checks require an execution handoff")
        try:
            checks = validate_completion_check(
                action.get("completion_check"), self._handoff.completion_conditions)
        except ValueError as exc:
            error = ValueError(str(exc))
            error.category = "completion_check_error"
            raise error from None
        action["completion_check"] = [dict(item) for item in checks]
        # The host owns the durable condition state.  A completion check is
        # accepted only after the strict coverage/status/evidence validation
        # above, then its condition IDs are translated to the Planner's
        # condition text before the terminal action is recorded.
        self.update_condition_status({
            condition: "satisfied"
            for condition, _check in zip(self._handoff.completion_conditions, checks)
        })
        self._emit("conditions_satisfied_from_completion_check",
                   conditions=list(self._handoff.completion_conditions))

    def _extract_condition_updates(self, action, scope):
        """Extract model claims for host verification before device execution."""
        if scope != "execution" or self._handoff is None:
            if "condition_updates" in action:
                raise ValueError("condition_updates are only allowed during execution recovery")
            return ()
        raw = action.pop("condition_updates", None)
        if self.condition_updates_enabled is False:
            if raw is not None:
                self._emit("condition_updates_ignored", reason="disabled")
            return ()
        if raw is None:
            if self.condition_updates_enabled is True:
                self._emit("condition_updates_missing")
            return ()
        try:
            updates = validate_condition_updates(raw, self._handoff.completion_conditions)
            self._emit("condition_updates_received", count=len(updates))
            return updates
        except (TypeError, ValueError) as exc:
            # This is optional advisory metadata. A malformed claim must not
            # invalidate an otherwise executable GUI action, and host state
            # remains pending because no reliable claim was received.
            self._emit("condition_updates_rejected", reason=type(exc).__name__)
            return ()

    def _fall_back(self, reason):
        self.phase = "fallback_execution"
        self._handoff = None
        self._fallback_reason = reason
        self._execution_cloud_calls = self.cloud_calls
        self._emit("fallback", reason=reason)

    def _request(self, current):
        # Retain initial boundary images plus the last two verification views.
        # The latest normal observation is always explicitly labeled current.
        ordered = [self._pre_frame, self._return_frame, *self._verification_frames[-2:], current]
        frames = {f.evidence_id: f for f in ordered if f is not None}
        return PlanRequest(
            original_goal=self.original_goal, public_constraints=self.public_constraints,
            history=self.history, frames=tuple(frames.values()), current_frame_id=current.evidence_id,
            decisions_used=self.decisions_used, decisions_remaining=self.remaining_decisions,
            verification_rounds_remaining=(self.budget.max_verification_rounds
                                           - self.verification_rounds_used),
            cloud_calls_remaining_after_this=(self.budget.max_cloud_calls - self.cloud_calls),
            verification=self._verification, verification_report=self._verification_report,
            verification_failed=self._verification_failed,
            pre_handoff_frame_id=self._pre_frame.evidence_id if self._pre_frame else None,
            total_decisions=self.budget.total_decisions)

    def _prepare(self, frame):
        if self.phase not in {"preparing", "reviewing"}:
            raise LifecycleError("Cloud analysis is limited to recovery preparation")
        if self.cloud_calls >= self.budget.max_cloud_calls:
            self._fall_back("cloud_call_limit")
            return
        self.cloud_calls += 1  # Failed calls still consume the preparation limit.
        request = self._request(frame)
        started = time.monotonic()
        record = {"call_number": self.cloud_calls, "current_frame_id": frame.evidence_id,
                  "evidence_ids": [f.evidence_id for f in request.frames]}
        was_reviewing = self.phase == "reviewing"
        try:
            from handoff_recovery.planner import validate_plan
            reply = self.planner.plan(request)
            if not isinstance(reply, PlanReply):
                raise TypeError("Planner must return a PlanReply")
            validate_plan(reply.plan, request)
            plan = reply.plan
            record["plan"] = asdict(plan)
            completion_status = getattr(plan, "completion_status", "INCOMPLETE")
            self._planner_completion_status = completion_status
            record["completion_status"] = completion_status
            # Usage metadata only, never raw response bodies or credentials.
            record["usage"] = {key: value for key, value in reply.usage.items()
                               if isinstance(key, str) and type(value) is int and value >= 0}
            if isinstance(plan, SlimPlan):
                if plan.decision == "VERIFY":
                    self._verification = VerificationTask(
                        plan.objective, plan.completion_conditions,
                        ("Read only; do not edit, save, delete, or change task data.",),
                        final_confirmation=(completion_status == "COMPLETE"),
                    )
                    self._initialize_condition_status(plan.completion_conditions)
                    self._final_confirmation_pending = completion_status == "COMPLETE"
                elif plan.decision == "EXECUTE":
                    self._handoff = Handoff(
                        plan.objective, (), plan.completion_conditions)
                    self._initialize_condition_status(plan.completion_conditions)
            else:
                if plan.decision == "VERIFY" and plan.verification is not None:
                    self._initialize_condition_status(plan.verification.required_evidence)
                elif plan.decision == "EXECUTE" and plan.handoff is not None:
                    self._initialize_condition_status(plan.handoff.completion_conditions)
            if plan.decision == "VERIFY":
                verification = (self._verification if isinstance(plan, SlimPlan)
                                else plan.verification)
                self._verification = verification
                self.verification_rounds_used += 1
                self.phase = "verifying"
                event = ("final_confirmation_assigned"
                         if verification.final_confirmation else "verification_assigned")
                self._emit(event, objective=verification.objective)
            elif plan.decision == "EXECUTE":
                handoff = (self._handoff if isinstance(plan, SlimPlan) else plan.handoff)
                self._handoff = handoff
                self._initialize_condition_status(handoff.completion_conditions)
                self.phase = "executing"
                self._execution_cloud_calls = self.cloud_calls
                self._emit("remaining_task_handed_off", objective=handoff.remaining_objective)
            else:
                # Accept explicit insufficiency for compatibility; the controller
                # owns the actual fallback transition, including API failures.
                self._fall_back("planner_could_not_form_reliable_guidance")
        except Exception as exc:
            metadata = error_metadata(exc, default_category="planner_error", source="planner")
            record.update(metadata)
            self._fall_back("planner_unavailable_or_invalid")
        finally:
            record["elapsed_seconds"] = round(time.monotonic() - started, 6)
            self._cloud_records.append(record)

    def _invalid_decision(self, scope, frame, **metadata):
        action = {"action": "invalid_decision", **metadata}
        self._history.append(OwnAction(self.decisions_used, action, scope, frame.evidence_id))
        self._emit("local_decision", scope=scope, frame_id=frame.evidence_id, action=action)
        return StepResult("invalid_decision", scope, self.decisions_used, None)

    def step(self, frame):
        """Issue at most one local decision; never execute a device action here."""
        if self.finished:
            raise LifecycleError("Task has ended")
        if self.phase == "paused":
            raise LifecycleError("Observation and decisions are paused during user control")
        if self._awaiting_action:
            raise LifecycleError("Acknowledge the pending device action before another decision")
        if self.remaining_decisions == 0:
            return self._finish("budget_exhausted")
        if not isinstance(frame, PublicFrame):
            raise TypeError("A public frame is required")
        if frame.evidence_id in self._decision_frames:
            raise LifecycleError("Each local decision needs a newly captured frame ID")
        self._observe(frame)
        if (self.phase in {"verifying", "executing"}
                and self._last_action_signature is not None
                and self._last_action_signature[0] in self._REPEAT_GUARD_ACTIONS
                and self._repeat_action_count >= 2
                and self._last_action_frame_digest == self._frame_digest(frame)):
            self._fall_back("repeated_action_without_state_change")
            self._repeat_action_count = 0
        if self.phase in {"preparing", "reviewing"}:
            self._prepare(frame)
        scope = self._local_scope()
        if self._execution_cloud_calls is not None and self.cloud_calls != self._execution_cloud_calls:
            raise LifecycleError("Cloud calls after execution handoff are forbidden")
        instruction = LocalInstruction(
            self.original_goal, self.public_constraints, scope,
            verification=self._verification if scope == "verification" else None,
            handoff=self._handoff if scope == "execution" else None,
            resume_original=(scope == "fallback"),
            resume_reason=(self._fallback_reason if scope == "fallback" else None),
            progress=self._progress,
            condition_status=self._condition_status,
            last_action_feedback=deepcopy(self._last_action_feedback),
            completion_policy=self.completion_policy,
            condition_updates_enabled=self.condition_updates_enabled)
        self.decisions_used += 1  # Count attempts, errors and scoped completion too.
        self._decision_frames.add(frame.evidence_id)
        if scope == "verification":
            self._verification_frames.append(frame)
        try:
            action = self.executor.decide(frame, instruction)
            if isinstance(action, ExecutorNoOp):
                # Some HandoffBench-native agents emit a complete assistant
                # message without a tool call. Count it like the benchmark's
                # invalid_decision event, without inventing a device action.
                return self._invalid_decision(scope, frame)
            if not isinstance(action, dict) or action.get("action") not in self._ACTIONS:
                raise ValueError("Executor did not return a supported public action")
            self._validate_completion_action(action, scope)
            condition_updates = self._extract_condition_updates(action, scope)
            if scope == "execution" and action.get("action") == "done":
                # A terminal model response may carry the final condition
                # claims. Apply those claims before checking terminality;
                # otherwise the last conditions can never leave ``pending``
                # because the response that declares them is rejected first.
                if (condition_updates
                        and self.condition_update_policy == "model_claim"):
                    claimed = {
                        item["condition"]: "satisfied"
                        for item in condition_updates
                    }
                    self.update_condition_status(claimed)
                    self._emit("model_condition_claims_applied",
                               conditions=list(claimed))
                pending = tuple(condition for condition, status in self._condition_status
                                if status == "pending")
                if pending and self.completion_policy == "conditions":
                    return self._reject_pending_done(
                        scope, frame, reason="pending_completion_conditions", pending=pending)
            own = OwnAction(self.decisions_used, action, scope, frame.evidence_id)
        except Exception as exc:
            self._pending_condition_updates = ()
            metadata = error_metadata(exc, default_category="executor_contract_error",
                                      source="executor")
            error_category = metadata["error_category"]
            error_class = metadata["error_class"]
            if (getattr(exc, "recoverable", False) is True
                    and error_category in {"native_parse_error", "action_mapping_error"}):
                return self._invalid_decision(scope, frame, **metadata)
            self._history.append(OwnAction(self.decisions_used,
                                          {"action": "decision_error", "error_type": type(exc).__name__,
                                           **({"error_category": error_category}
                                              if error_category is not None else {}),
                                           "error_class": error_class},
                                          scope, frame.evidence_id))
            return self._finish("agent_error", error_type=metadata["error_type"],
                                error_category=error_category, error_class=error_class)
        self._history.append(own)
        signature = self._action_signature(action)
        digest = self._frame_digest(frame)
        if (scope in {"verification", "execution"} and signature is not None
                and signature == self._last_action_signature
                and digest == self._last_action_frame_digest):
            self._repeat_action_count += 1
        else:
            self._repeat_action_count = 1 if scope in {"verification", "execution"} else 0
        self._last_action_signature = signature
        self._last_action_frame_digest = digest
        self._last_action_feedback = {
            "action": action.get("action"), "target": action.get("target", ""),
            "result": "pending", "state_changed": None,
        }
        self._emit("local_decision", scope=scope, frame_id=frame.evidence_id, action=action)
        kind = action["action"]
        if scope == "verification" and kind in {"done", "fail"}:
            self._pending_condition_updates = ()
            completed = kind == "done"
            self._verification_report = ("Verification executor reported completion." if completed
                                         else "Verification executor reported it could not complete the check.")
            self._verification_failed = not completed
            if self._final_confirmation_pending:
                self._final_confirmation_pending = False
                if completed:
                    # A COMPLETE assessment is resolved by one read-only
                    # confirmation. The benchmark performs the authoritative
                    # final task evaluation after this terminal signal.
                    self._emit("final_confirmation_returned", completed=True)
                    return self._finish("task_complete", action=action)
                # A failed COMPLETE assessment must be replanned as execution
                # or fallback; it must not start another verification loop.
                self._emit("final_confirmation_returned", completed=False)
            # Deliberately do NOT call the planner or executor again here.
            self.phase = "reviewing"
            self._emit("verification_returned", completed=completed)
            if self.remaining_decisions == 0:
                return self._finish("budget_exhausted", action=action)
            return StepResult("verification_complete" if completed else "verification_failed",
                              scope, self.decisions_used, deepcopy(action))
        if kind == "done":
            self._pending_condition_updates = ()
            return self._finish("task_complete", action=action)
        if kind == "fail":
            self._pending_condition_updates = ()
            return self._finish("agent_failed", action=action)
        self._pending_condition_updates = condition_updates
        self._awaiting_action = True
        return StepResult("action", scope, self.decisions_used, deepcopy(action),
                          condition_updates=condition_updates)

    def acknowledge_action(self, *, success, state_changed=None, satisfied_conditions=(),
                           model_claimed_conditions=()):
        """The external host reports actual device execution, never a score."""
        if not self._awaiting_action or self.finished:
            raise LifecycleError("No pending device action to acknowledge")
        if type(success) is not bool:
            raise TypeError("Action execution success must be boolean")
        if state_changed is not None and type(state_changed) is not bool:
            raise TypeError("state_changed must be a boolean or None")
        if model_claimed_conditions and self.condition_update_policy != "model_claim":
            raise LifecycleError("Model condition claims require model_claim policy")
        declared_conditions = tuple(
            item["condition"] for item in self._pending_condition_updates
            if item.get("status") == "satisfied"
        )
        if (model_claimed_conditions
                and not set(model_claimed_conditions).issubset(set(declared_conditions))):
            raise LifecycleError("Host cannot apply undeclared model condition claims")
        if satisfied_conditions:
            if self.condition_update_policy == "host":
                self.update_condition_status({condition: "satisfied"
                                              for condition in satisfied_conditions})
            else:
                # In the default model-claim mode semantic host receipts are
                # deliberately ignored. The host still acknowledges whether
                # the device action executed successfully.
                self._emit("host_condition_receipt_ignored",
                           conditions=list(satisfied_conditions))
        if success and self.condition_update_policy == "model_claim" and declared_conditions:
            self.update_condition_status({condition: "satisfied"
                                          for condition in declared_conditions})
            self._emit("model_condition_claims_applied",
                       conditions=list(declared_conditions))
        self._pending_condition_updates = ()
        self._awaiting_action = False
        if self._history and self._history[-1].decision_number == self.decisions_used:
            last = self._history[-1]
            self._history[-1] = replace(last, result="success" if success else "failed",
                                        state_changed=state_changed)
            self._last_action_feedback = {
                "action": last.action.get("action"),
                "target": last.action.get("target", ""),
                "result": "success" if success else "failed",
                "state_changed": state_changed,
            }
        self._emit("action_acknowledged", success=success)
        if not success:
            return self._finish("device_error")
        if self.remaining_decisions == 0:
            return self._finish("budget_exhausted")
        return None

    def report(self):
        """Diagnostics only; a completion declaration does not assign success."""
        return deepcopy({
            "scope": "standalone_prototype_not_benchmark_evaluation", "task_id": self.task_id,
            "original_goal": self.original_goal, "public_constraints": list(self.public_constraints),
            "phase": self.phase, "termination_reason": self.terminal_reason,
            "error_category": next(
                (event.get("error_category") for event in reversed(self._events)
                 if event.get("error_category")),
                next((record.get("error_category") for record in reversed(self._cloud_records)
                      if record.get("error_category")), None)),
            "error_type": next(
                (event.get("error_type") for event in reversed(self._events)
                 if event.get("error_type")),
                next((record.get("error_type") for record in reversed(self._cloud_records)
                      if record.get("error_type")), None)),
            "error_class": next(
                (event.get("error_class") for event in reversed(self._events)
                 if event.get("error_class")),
                next((record.get("error_class") for record in reversed(self._cloud_records)
                      if record.get("error_class")), None)),
            "task_success": None, "score_source": "external_benchmark_required",
            "budget": {"total": self.budget.total_decisions, "used": self.decisions_used,
                       "remaining": self.remaining_decisions},
            "budget_semantics": deepcopy(GUI_BUDGET_SEMANTICS),
            "budget_policy": {"max_cloud_calls": self.budget.max_cloud_calls,
                               "max_verification_rounds": self.budget.max_verification_rounds},
            "cloud_calls": self.cloud_calls,
            "recovery_features": {
                "completion_policy": self.completion_policy,
                "condition_updates_enabled": (
                    self.condition_updates_enabled if self.condition_updates_enabled is not None
                    else self.completion_policy == "conditions"
                    and getattr(self.executor, "condition_updates_protocol", "json_field_v1") == "json_field_v1"),
                "condition_updates_requested": self.condition_updates_enabled,
                "condition_updates_protocol": getattr(self.executor, "condition_updates_protocol", "json_field_v1"),
                "structured_progress": True,
                "progress_delivery": "always",
                "progress_fields": ["confirmed", "uncertain"],
                "condition_update_policy": self.condition_update_policy,
                "model_claims_apply_after_successful_action": (
                    self.condition_update_policy == "model_claim"),
            },
            "progress": {"confirmed": list(self._progress.confirmed),
                         "uncertain": list(self._progress.uncertain)},
            "condition_status": [{"condition": condition, "status": status}
                                 for condition, status in self._condition_status],
            "condition_update_counts": {
                "declared_conditions": sum(event.get("count", 0) for event in self._events
                                           if event["event"] == "condition_updates_received"),
                **{name: sum(event["event"] == "condition_updates_" + name for event in self._events)
                   for name in ("received", "missing", "rejected", "ignored")}},
            "repeat_action_guard": {
                "enabled": True,
                "threshold": 2,
                "repeat_count": self._repeat_action_count,
                "last_transition": self._fallback_reason
                    if self._fallback_reason == "repeated_action_without_state_change" else None,
            },
            "history_policy": self._history_policy_descriptor(),
            "fallback": {
                "resume_original_context": True,
                "reason": self._fallback_reason,
            },
            "cloud_calls_after_handoff": (0 if self._execution_cloud_calls is None
                                          else self.cloud_calls - self._execution_cloud_calls),
            "verification_rounds": self.verification_rounds_used,
            "cloud_requests": self._cloud_records, "own_history": [asdict(a) for a in self._history],
            "observations": [f.metadata() for f in self._frames.values()], "events": self._events})
