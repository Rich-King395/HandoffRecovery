"""Opt-in HandoffBench agent using only its public control protocol.

The host executes every action and owns authoritative budgets and scoring.
Callbacks perform bookkeeping only. Each decide performs at most one local
model request; verification completion is returned to the host explicitly.
"""
from __future__ import annotations

from copy import deepcopy
from importlib import import_module

from handoff_recovery.core.coordinator import LifecycleError, RecoveryCoordinator
from handoff_recovery.core.protocols import validate_executor
from handoff_recovery.core.types import PublicFrame, RecoveryBudget
from .loader import BenchmarkImportError, load_gui_owl_bindings


class HandoffRecoveryAgent:
    public_protocol_version = "handoff_v1"
    retain_full_public_history = True

    def __init__(self, *, bench_path, planner, executor, public_constraints=(),
                 include_prior_progress=True, action_validator=None):
        # Built-in adapters retain the historical GUI-Owl normalized-action
        # validator. Custom executors may supply their own validator so this
        # bridge does not depend on a concrete GUI Agent implementation.
        self._bindings = (load_gui_owl_bindings(bench_path)
                          if action_validator is None else None)
        if action_validator is not None and not callable(action_validator):
            raise TypeError("action_validator must be callable")
        validate_executor(executor)
        self._action_validator = (action_validator if action_validator is not None
                                  else self._bindings.validate_action)
        try:
            self._protocol = import_module("resumebench.core.agent_protocol")
        except ImportError:
            raise BenchmarkImportError("Benchmark lacks the public handoff_v1 protocol") from None
        if self._protocol.PUBLIC_PROTOCOL_VERSION != self.public_protocol_version:
            raise BenchmarkImportError("Benchmark public handoff protocol is incompatible")
        if not isinstance(public_constraints, tuple):
            raise TypeError("Public constraints must be an explicit tuple")
        self._planner = planner
        self._executor = executor
        self._constraints = public_constraints
        self._include_prior_progress = include_prior_progress
        self.reset()

    def reset(self):
        """Called once by the host before its public task-start callback."""
        self.coordinator = None
        self._decision_context = None
        self._pending_return = None
        self._pending_action = None
        self._frame_number = 0

    def _require_started(self):
        if self.coordinator is None:
            raise LifecycleError("The public task-start callback is required")
        return self.coordinator

    def _check_total(self, total):
        coordinator = self._require_started()
        if type(total) is not int or total != coordinator.budget.total_decisions:
            raise LifecycleError("Host and recovery decision budgets differ")

    def on_task_start(self, context):
        if not isinstance(context, self._protocol.TaskStart):
            raise TypeError("A public TaskStart is required")
        if self.coordinator is not None:
            raise LifecycleError("Reset before starting a new task")
        if type(context.max_decisions) is not int or context.max_decisions < 1:
            raise LifecycleError("Host must provide a positive decision budget")
        self.coordinator = RecoveryCoordinator(
            task_id=str(context.task_id), original_goal=context.goal,
            public_constraints=self._constraints, planner=self._planner, executor=self._executor,
            include_prior_progress=self._include_prior_progress,
            budget=RecoveryBudget(total_decisions=context.max_decisions))

    @staticmethod
    def _observation_identity(observation):
        if not isinstance(observation, dict):
            raise TypeError("A public observation dictionary is required")
        return tuple(observation.get(key) for key in ("screenshot", "width", "height"))

    def _frame(self, observation):
        path, width, height = self._observation_identity(observation)
        if not isinstance(path, str) or not path:
            raise ValueError("A normal public screenshot path is required")
        self._frame_number += 1
        frame = PublicFrame.from_png(f"public_{self._frame_number}", path)
        if (frame.width, frame.height) != (width, height):
            raise ValueError("Public screenshot dimensions differ from the observation")
        return frame

    def on_control_event(self, event):
        coordinator = self._require_started()
        if not isinstance(event, self._protocol.ControlEvent):
            raise TypeError("A public ControlEvent is required")
        self._check_total(event.max_decisions)
        if event.decisions_used != coordinator.decisions_used:
            raise LifecycleError("Host and recovery decision counts differ")
        if self._decision_context is not None or self._pending_action is not None:
            raise LifecycleError("Control cannot change during a pending decision or device action")
        if event.kind == "suspended":
            # The coordinator already holds its last own observation. In
            # particular, an initial takeover has no pre-handoff screenshot;
            # never request or substitute a private initial/checker capture.
            coordinator.pause_for_handoff(reopen_original_completion=coordinator.finished)
        elif event.kind == "returned":
            frame = self._frame(event.observation)
            coordinator.return_control(frame)
            self._pending_return = (self._observation_identity(event.observation), frame)
        else:
            raise LifecycleError("Unknown public control event")

    def on_decision_start(self, context):
        coordinator = self._require_started()
        if not isinstance(context, self._protocol.DecisionContext):
            raise TypeError("A public DecisionContext is required")
        self._check_total(context.max_decisions)
        if self._decision_context is not None:
            raise LifecycleError("The preceding decision has not been consumed")
        if (context.decisions_used != coordinator.decisions_used
                or context.decision_number != coordinator.decisions_used + 1):
            raise LifecycleError("Host and recovery decision counts differ")
        # HandoffBench may apply a smaller prefix/resume phase cap while the
        # shared task budget remains larger. The host owns that phase cap;
        # recovery only verifies it is positive and within the shared budget.
        if (type(context.decisions_remaining) is not int
                or not 1 <= context.decisions_remaining <= coordinator.remaining_decisions):
            raise LifecycleError("Host phase budget exceeds the shared decision budget")
        self._decision_context = context

    def decide(self, observation, goal, history):
        """Return one ordinary action or one explicit scoped completion."""
        coordinator = self._require_started()
        context = self._decision_context
        if context is None:
            raise LifecycleError("The host must announce every counted decision")
        self._decision_context = None
        if goal != coordinator.original_goal:
            raise LifecycleError("The original public goal changed within the task")
        if self._pending_return is not None:
            identity, frame = self._pending_return
            if self._observation_identity(observation) != identity:
                raise LifecycleError("Use the same fresh public return observation for the first decision")
            self._pending_return = None
        else:
            frame = self._frame(observation)
        # All own text history is retained by the coordinator and all native
        # outputs by the executor. Do not inspect host history/private records.
        result = coordinator.step(frame)
        if coordinator.decisions_used != context.decision_number:
            raise LifecycleError("A decision must consume exactly one host budget unit")
        if result.kind == "agent_error":
            category = result.error_category or "unclassified_error"
            error_class = result.error_class or "gui_agent_error"
            error = RuntimeError(
                f"Local recovery executor failed ({error_class}: {category}; {result.error_type})")
            error.category = category
            error.error_class = error_class
            raise error
        if result.kind == "invalid_decision":
            # Match HandoffBench's native-agent behavior: the response
            # consumed one decision but produced no device action.
            return None
        action = result.action
        if not isinstance(action, dict):
            raise LifecycleError("The coordinator did not return a counted decision")
        self._action_validator(action, frame.width, frame.height, allow_done=True)
        if result.scope == "verification" and action.get("action") in {"done", "fail"}:
            # Includes done at decision 20: it is still verification-only and
            # must never become an original-task completion or a free wait.
            return self._protocol.SubtaskCompletion(action=deepcopy(action), scope="verification")
        if result.kind == "action":
            self._pending_action = context.decision_number
        return deepcopy(action)

    def on_action_result(self, result):
        coordinator = self._require_started()
        if not isinstance(result, self._protocol.ActionResult):
            raise TypeError("A public ActionResult is required")
        if (self._pending_action is None or result.decision_number != self._pending_action
                or result.decision_number != coordinator.decisions_used):
            raise LifecycleError("Device acknowledgement does not match the pending action")
        self._pending_action = None
        coordinator.acknowledge_action(success=result.success)

    def report(self):
        """Recovery diagnostics; all task/recovery scores come from HandoffBench."""
        report = self._require_started().report()
        report["scope"] = "handoffbench_public_protocol"
        report["public_protocol_version"] = self.public_protocol_version
        report["score_source"] = "external_handoffbench_result"
        report["budget_semantics"] = {
            **report.get("budget_semantics", {}),
            "host_total_budget": "HandoffBench budgets.task",
            "host_phase_caps": "prefix/resume caps can shorten a phase but never refill total",
        }
        return report
