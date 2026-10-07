"""Bounded recovery preparation followed by local-only execution.

This is an independent prototype protocol, not itself a run_task agent.
Each step requests at most one local decision. GUI actions require an explicit
host acknowledgement; scoped verification completion is returned to the host
without another local decision hidden inside that call. The host owns device
execution and scoring. The public Benchmark bridge preserves this accounting.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import time

from .types import (
    HANDOFFBENCH_ACTIONS, Handoff, LocalInstruction, OwnAction, PlanReply, PlanRequest,
    PublicFrame,
    RecoveryBudget, SlimPlan, StepResult, VerificationTask, validate_completion_check,
)
from .protocols import ExecutorNoOp
from .protocols import ExecutorProtocol, PlannerProtocol, validate_executor


class LifecycleError(ValueError):
    """A host call violates the explicit observation/control protocol."""


class RecoveryCoordinator:
    # Keep the recovery contract aligned with HandoffBench's complete public
    # action vocabulary. The host still performs full field/value validation.
    _ACTIONS = HANDOFFBENCH_ACTIONS

    def __init__(self, *, task_id, original_goal, planner, executor, public_constraints=(),
                 include_prior_progress=True, budget=None):
        # Validate the public goal/constraints before starting a task.
        LocalInstruction(original_goal, tuple(public_constraints), "original")
        if not isinstance(task_id, str) or not task_id.strip():
            raise ValueError("task_id must be nonempty text")
        self.task_id = task_id
        self.original_goal = original_goal
        self.public_constraints = tuple(public_constraints)
        if type(include_prior_progress) is not bool:
            raise TypeError("include_prior_progress must be a boolean")
        self.include_prior_progress = include_prior_progress
        self.planner: PlannerProtocol = planner
        self.executor: ExecutorProtocol = validate_executor(executor)
        self.budget = budget if budget is not None else RecoveryBudget()
        if not isinstance(self.budget, RecoveryBudget):
            raise TypeError("budget must be a RecoveryBudget")
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
        self._handoff = None
        self._awaiting_action = False
        self._events = []
        self._cloud_records = []
        self._execution_cloud_calls = None
        self._fallback_reason = None
        self.executor.start_task(task_id, original_goal)
        self._emit("task_started", budget=self.budget.total_decisions)

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

    def _finish(self, reason, *, action=None, error_type=None, error_category=None):
        scope = self._local_scope()
        self.terminal_reason = reason
        self.phase = "finished"
        self._awaiting_action = False
        self._emit("task_terminated", reason=reason, error_type=error_type,
                   **({"error_category": error_category} if error_category else {}))
        return StepResult(reason, scope, self.decisions_used or None, deepcopy(action), error_type,
                          error_category)

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

    def _progress_summary(self):
        """Return bounded, semantic progress facts for the next local prompt."""
        items = []
        for record in self._history:
            if record.scope != "original":
                continue
            action = record.action
            kind = action.get("action", "unknown")
            fact = f"decision {record.decision_number}: {kind} on {record.frame_id}"
            thought = action.get("thought")
            if isinstance(thought, str) and thought.strip():
                fact += f"; model note: {thought.strip()[:240]}"
            items.append(fact)
        return tuple(items[-6:])

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
        try:
            from handoff_recovery.planner import validate_plan
            reply = self.planner.plan(request)
            if not isinstance(reply, PlanReply):
                raise TypeError("Planner must return a PlanReply")
            validate_plan(reply.plan, request)
            plan = reply.plan
            record["plan"] = asdict(plan)
            # Usage metadata only, never raw response bodies or credentials.
            record["usage"] = {key: value for key, value in reply.usage.items()
                               if isinstance(key, str) and type(value) is int and value >= 0}
            if isinstance(plan, SlimPlan):
                if plan.decision == "VERIFY":
                    self._verification = VerificationTask(
                        plan.objective, plan.completion_conditions, plan.constraints)
                elif plan.decision == "EXECUTE":
                    self._handoff = Handoff(
                        plan.objective, plan.constraints, plan.completion_conditions)
            if plan.decision == "VERIFY":
                verification = (self._verification if isinstance(plan, SlimPlan)
                                else plan.verification)
                self._verification = verification
                self.verification_rounds_used += 1
                self.phase = "verifying"
                self._emit("verification_assigned", objective=verification.objective)
            elif plan.decision == "EXECUTE":
                handoff = (self._handoff if isinstance(plan, SlimPlan) else plan.handoff)
                self._handoff = handoff
                self.phase = "executing"
                self._execution_cloud_calls = self.cloud_calls
                self._emit("remaining_task_handed_off", objective=handoff.remaining_objective)
            else:
                # Accept explicit insufficiency for compatibility; the controller
                # owns the actual fallback transition, including API failures.
                self._fall_back("planner_could_not_form_reliable_guidance")
        except Exception as exc:
            record["error_type"] = type(exc).__name__
            category = getattr(exc, "category", None)
            if isinstance(category, str) and category.isidentifier():
                record["error_category"] = category
            self._fall_back("planner_unavailable_or_invalid")
        finally:
            record["elapsed_seconds"] = round(time.monotonic() - started, 6)
            self._cloud_records.append(record)

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
        if self.phase in {"preparing", "reviewing"}:
            self._prepare(frame)
        scope = self._local_scope()
        if self._execution_cloud_calls is not None and self.cloud_calls != self._execution_cloud_calls:
            raise LifecycleError("Cloud calls after execution handoff are forbidden")
        instruction = LocalInstruction(
            self.original_goal, self.public_constraints, scope,
            verification=self._verification if scope == "verification" else None,
            handoff=self._handoff if scope == "execution" else None,
            prior_progress=(self._progress_summary()
                            if self.include_prior_progress and scope in {"execution", "fallback"}
                            else ()),
            resume_original=(scope == "fallback"),
            resume_reason=(self._fallback_reason if scope == "fallback" else None))
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
                self._history.append(OwnAction(self.decisions_used,
                                              {"action": "invalid_decision"},
                                              scope, frame.evidence_id))
                self._emit("local_decision", scope=scope, frame_id=frame.evidence_id,
                           action={"action": "invalid_decision"})
                return StepResult("invalid_decision", scope, self.decisions_used, None)
            if not isinstance(action, dict) or action.get("action") not in self._ACTIONS:
                raise ValueError("Executor did not return a supported public action")
            self._validate_completion_action(action, scope)
            own = OwnAction(self.decisions_used, action, scope, frame.evidence_id)
        except Exception as exc:
            error_category = getattr(exc, "category", None)
            if not isinstance(error_category, str) or not error_category:
                error_category = "executor_contract_error"
            self._history.append(OwnAction(self.decisions_used,
                                          {"action": "decision_error", "error_type": type(exc).__name__,
                                           **({"error_category": error_category}
                                              if error_category is not None else {})},
                                          scope, frame.evidence_id))
            return self._finish("agent_error", error_type=type(exc).__name__,
                                error_category=error_category)
        self._history.append(own)
        self._emit("local_decision", scope=scope, frame_id=frame.evidence_id, action=action)
        kind = action["action"]
        if scope == "verification" and kind in {"done", "fail"}:
            completed = kind == "done"
            self._verification_report = ("Verification executor reported completion." if completed
                                         else "Verification executor reported it could not complete the check.")
            # Deliberately do NOT call the planner or executor again here.
            self.phase = "reviewing"
            self._emit("verification_returned", completed=completed)
            if self.remaining_decisions == 0:
                return self._finish("budget_exhausted", action=action)
            return StepResult("verification_complete" if completed else "verification_failed",
                              scope, self.decisions_used, deepcopy(action))
        if kind == "done":
            return self._finish("task_complete", action=action)
        if kind == "fail":
            return self._finish("agent_failed", action=action)
        self._awaiting_action = True
        return StepResult("action", scope, self.decisions_used, deepcopy(action))

    def acknowledge_action(self, *, success):
        """The external host reports actual device execution, never a score."""
        if not self._awaiting_action or self.finished:
            raise LifecycleError("No pending device action to acknowledge")
        if type(success) is not bool:
            raise TypeError("Action execution success must be boolean")
        self._awaiting_action = False
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
            "task_success": None, "score_source": "external_benchmark_required",
            "budget": {"total": self.budget.total_decisions, "used": self.decisions_used,
                       "remaining": self.remaining_decisions},
            "budget_policy": {"max_cloud_calls": self.budget.max_cloud_calls,
                               "max_verification_rounds": self.budget.max_verification_rounds},
            "cloud_calls": self.cloud_calls,
            "recovery_features": {"structured_progress": self.include_prior_progress},
            "fallback": {
                "resume_original_context": True,
                "reason": self._fallback_reason,
            },
            "cloud_calls_after_handoff": (0 if self._execution_cloud_calls is None
                                          else self.cloud_calls - self._execution_cloud_calls),
            "verification_rounds": self.verification_rounds_used,
            "cloud_requests": self._cloud_records, "own_history": [asdict(a) for a in self._history],
            "observations": [f.metadata() for f in self._frames.values()], "events": self._events})
