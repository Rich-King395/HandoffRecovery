"""Small extension points for model-specific adapters.

Concrete Planner and GUI Agent implementations may use any transport or
native protocol as long as they expose these normalized methods.
"""
from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import json
from typing import Any, Protocol, runtime_checkable

from .types import LocalInstruction, PlanReply, PlanRequest, PublicFrame


HISTORY_POLICY_VERSION = "recovery_history_v3"
RECOVERY_HISTORY_MODES = frozenset({"isolated", "original_snapshot"})
DEFAULT_RECOVERY_HISTORY_MODE = "isolated"
HISTORY_POLICY = {
    "version": HISTORY_POLICY_VERSION,
    "original": "retain_original_scope_history",
    "recovery": "start_empty_recovery_scope_history_by_default",
    "same_scope": "retain_only_current_scope_native_history",
    "scope_transition": "snapshot_previous_scope_before_switch",
    "fallback": "restore_original_scope_history",
    "handoff_context": "structured_recovery_context_outside_native_history",
    "invalid_output": "do_not_append_failed_native_output",
    "recovery_history_mode": DEFAULT_RECOVERY_HISTORY_MODE,
    "available_recovery_history_modes": sorted(RECOVERY_HISTORY_MODES),
}

GUI_AGENT_ERROR_CATEGORIES = frozenset({
    "request_error", "response_error", "native_parse_error", "action_mapping_error",
    "history_error", "prompt_error", "completion_check_error", "executor_contract_error",
})

ERROR_CATEGORIES = frozenset({
    *GUI_AGENT_ERROR_CATEGORIES,
    "planner_error", "plan_validation_error", "planner_unavailable_or_invalid",
    "device_error", "environment_error", "lifecycle_error", "integration_error",
})

ERROR_CLASSES = frozenset({
    "gui_agent_error", "recovery_control_error", "device_error",
    "environment_error", "lifecycle_error",
})


RecoveryContext = dict[str, Any]

NATIVE_COMPLETION_GUIDANCE = (
    "Execute the recovery objective while preserving every original task requirement. "
    "Check all listed completion conditions against the visible state yourself, then use "
    "your existing native completion action. Do not add condition_updates or "
    "completion_check fields. Recorded condition statuses are diagnostics, not a "
    "prerequisite for completion. The independent task evaluator decides success."
)


def recovery_task_goal(original_goal: str, context: RecoveryContext | None) -> str:
    """Build the layered recovery task text while preserving the original goal."""
    if context is None:
        return original_goal
    if not isinstance(context, dict):
        raise TypeError("recovery_context must be a mapping or None")
    scope = context.get("scope")
    final_confirmation = False
    condition_status = context.get("condition_status", [])
    if not isinstance(condition_status, list):
        raise ValueError("recovery condition_status must be a list")
    if scope == "verification":
        task = context.get("verification")
        if not isinstance(task, dict):
            raise ValueError("verification recovery context is missing its task")
        objective = task.get("objective")
        conditions = task.get("required_evidence")
        final_confirmation = task.get("final_confirmation", False)
    elif scope == "execution":
        task = context.get("handoff")
        if not isinstance(task, dict):
            raise ValueError("execution recovery context is missing its handoff")
        objective = task.get("remaining_objective")
        conditions = task.get("completion_conditions")
    elif scope == "fallback":
        return original_goal
    else:
        raise ValueError("recovery_context scope must be verification, execution, or fallback")
    if (not isinstance(objective, str) or not objective.strip()
            or not isinstance(conditions, list)):
        raise ValueError("recovery context task fields are invalid")
    if scope == "verification":
        if final_confirmation:
            guidance = (
                "Perform only this final read-only confirmation. Do not edit, save, "
                "or delete task data. Return done when the original task's required "
                "final state is visible on screen."
            )
        else:
            guidance = (
                "Perform only this bounded read-only check. Do not edit, save, delete, "
                "or treat obtaining evidence as completion of the original task."
            )
    elif context.get("completion_policy") == "native":
        guidance = NATIVE_COMPLETION_GUIDANCE
    else:
        guidance = (
            "Execute this recovery objective from the visible screen while preserving "
            "the original task requirements. After a successful action, use "
            "condition_updates to mark a condition [satisfied] only when the latest "
            "screenshot provides visible evidence. Each update must be an object with "
            "condition_id (c1, c2, ...), status=\"satisfied\", and concise evidence. "
            "The JSON extension is \"condition_updates\": [{\"condition_id\": \"c1\", "
            "\"status\": \"satisfied\", \"evidence\": \"...\"}]. "
            "Use an empty condition_updates array when no condition became satisfied; "
            "omitting the field is treated the same way. Do not use any other status. "
            "Before returning done, check every completion_condition yourself. You may include "
            "newly satisfied conditions in condition_updates in the same done response; those "
            "claims are applied before the terminal check. Return done only when every condition "
            "in Completion condition status is marked [satisfied]."
        )
    if scope == "verification" and context.get("completion_policy") == "native":
        guidance = guidance.replace("Return done", "Use your existing native completion action")
    updates_enabled = context.get("condition_updates_enabled")
    if updates_enabled is not None and type(updates_enabled) is not bool:
        raise TypeError("condition_updates_enabled must be boolean or None")
    if scope == "execution" and updates_enabled is not None:
        if updates_enabled:
            guidance = (
                "Execute the recovery objective while preserving every original task requirement. "
                "Report newly satisfied conditions using condition_id (c1, c2, ...), "
                "status=\"satisfied\", and concise evidence from the current screenshot. "
                "Do not predict the outcome of the action you are about to perform. "
                "Use an empty condition_updates array or omit updates when no condition is newly satisfied. "
                "You may include the last updates with your existing native completion action. "
            )
            if context.get("completion_policy") == "native":
                guidance += ("Recorded statuses are diagnostics, not a prerequisite for completion. "
                             "Check the visible final state yourself; the independent evaluator decides success.")
            else:
                guidance += "Complete only when every condition is marked satisfied, including updates in this reply."
        elif context.get("completion_policy") != "native":
            guidance = ("Execute the recovery objective while preserving every original task requirement. "
                        "Do not output condition_updates. Check every completion condition yourself. "
                        "Complete only when every recorded condition is satisfied.")
        if context.get("completion_check_required"):
            guidance += (" With the native completion action, include completion_check covering every original "
                         "condition in order, each with condition_id (c1, c2, ...), status=satisfied, "
                         "and concise visible evidence.")
    status_lines = []
    pending = []
    if not condition_status:
        condition_status = [{"condition": condition, "status": "pending"}
                            for condition in conditions]
    for item in condition_status:
        if (not isinstance(item, dict) or set(item) != {"condition", "status"}
                or not isinstance(item["condition"], str)
                or item["status"] not in {"pending", "satisfied", "unverifiable"}):
            raise ValueError("recovery condition_status entries are invalid")
        label = ""
        if updates_enabled is True and scope == "execution":
            if item["condition"] not in conditions:
                raise ValueError("Condition status is not part of this recovery objective")
            label = f"c{conditions.index(item['condition']) + 1} "
        status_lines.append(f"- {label}[{item['status']}] {item['condition']}")
        if item["status"] == "pending":
            pending.append(item["condition"])
    # Keep action receipts internal so they cannot bypass native history isolation.
    return (
        "Original task (final goal and required values):\n" + original_goal
        + "\n\nA user takeover occurred while the original task was being executed. "
        + "The Planner has provided the recovery objective below."
        + "\n\nCurrent recovery objective (execute this now):\n" + objective
        + "\n\nCompletion condition status:\n"
        + "\n".join(status_lines or ["- [pending] No condition status recorded"])
        + "\n\nExecution guidance:\n- " + guidance
    )


def classify_error(category: str | None, *, source: str) -> str:
    if source == "planner" or category in {"planner_error", "plan_validation_error",
                                            "planner_unavailable_or_invalid"}:
        return "recovery_control_error"
    if source == "device" or category == "device_error":
        return "device_error"
    if source == "environment" or category == "environment_error":
        return "environment_error"
    if source == "lifecycle" or category == "lifecycle_error":
        return "lifecycle_error"
    if category == "integration_error":
        return "lifecycle_error"
    if category in GUI_AGENT_ERROR_CATEGORIES or source == "executor":
        return "gui_agent_error"
    # Unknown host/control failures must not silently become GUI Agent errors.
    return "recovery_control_error"


def error_metadata(exc: BaseException, *, default_category: str,
                   source: str) -> dict[str, str]:
    """Return bounded, comparable error metadata without exception text."""
    category = getattr(exc, "category", None)
    # Planner validation has deliberately fine-grained subcategories (for
    # example ``evidence_id_unavailable``). Keep those identifiers while the
    # top-level class remains ``recovery_control_error``.
    if (not isinstance(category, str) or not category.isidentifier()
            or (source != "planner" and category not in ERROR_CATEGORIES)):
        category = default_category
    return {
        "error_category": category,
        "error_class": classify_error(category, source=source),
        "error_type": type(exc).__name__,
    }


def apply_history_policy(contexts, current_history, previous_scope, instruction,
                         *, recovery_history_mode=DEFAULT_RECOVERY_HISTORY_MODE):
    """Apply the one native-history policy for every recovery Executor.

    ``isolated`` starts each recovery scope with an empty native history and
    retains only actions produced in that scope. ``original_snapshot`` is an
    explicit A/B mode that seeds recovery from the pre-takeover history.
    Fallback resumes the original native history in either mode.
    """
    if not isinstance(contexts, dict) or not isinstance(current_history, list):
        raise TypeError("History policy requires a context mapping and list history")
    if recovery_history_mode not in RECOVERY_HISTORY_MODES:
        raise ValueError("Unknown recovery history mode")
    if previous_scope is not None and previous_scope not in {
            "original", "verification", "execution", "fallback"}:
        raise ValueError("History policy received an unknown previous scope")
    effective_scope = "original" if instruction.resume_original else instruction.scope
    if effective_scope == previous_scope:
        return current_history, previous_scope
    if previous_scope is not None:
        contexts[previous_scope] = deepcopy(current_history)
    if effective_scope in {"verification", "execution"}:
        source = contexts.get("original", []) if recovery_history_mode == "original_snapshot" else []
    else:
        source = contexts.get("original", [])
    history = deepcopy(source)
    return history, effective_scope


def history_policy_descriptor(recovery_history_mode=DEFAULT_RECOVERY_HISTORY_MODE):
    if recovery_history_mode not in RECOVERY_HISTORY_MODES:
        raise ValueError("Unknown recovery history mode")
    descriptor = dict(HISTORY_POLICY)
    descriptor["recovery_history_mode"] = recovery_history_mode
    descriptor["include_original_history_in_recovery"] = recovery_history_mode == "original_snapshot"
    return descriptor


def history_policy_transition(previous_scope, instruction, *,
                              recovery_history_mode=DEFAULT_RECOVERY_HISTORY_MODE) -> dict:
    """Describe the host-owned native-history transition without model data."""
    if recovery_history_mode not in RECOVERY_HISTORY_MODES:
        raise ValueError("Unknown recovery history mode")
    effective_scope = "original" if instruction.resume_original else instruction.scope
    if effective_scope in {"verification", "execution"} and previous_scope != effective_scope:
        history_source = ("original_snapshot" if recovery_history_mode == "original_snapshot"
                          else "empty_recovery_scope")
    elif effective_scope == "original" and previous_scope != effective_scope:
        history_source = "original_snapshot"
    else:
        history_source = "current_scope"
    return {
        "policy_version": HISTORY_POLICY_VERSION,
        "recovery_history_mode": recovery_history_mode,
        "previous_scope": previous_scope,
        "effective_scope": effective_scope,
        "history_source": history_source,
        "recovery_context_in_native_history": False,
    }


class ExecutorNoOp:
    """Explicit native no-op returned when an agent emits no tool call."""

    __slots__ = ()


class PlannerProtocol(Protocol):
    def plan(self, request: PlanRequest) -> PlanReply:
        """Produce one normalized recovery plan."""


@runtime_checkable
class ExecutorProtocol(Protocol):
    """Normalized recovery-facing GUI Agent contract.

    The two lifecycle methods are mandatory. ``validate_action``,
    ``descriptor`` and ``requires_completion_check`` are optional capabilities
    checked by :func:`validate_executor`; this keeps lightweight test
    executors and agents with their own host validator compatible.
    """

    def start_task(self, task_id: str, original_goal: str) -> None:
        """Reset native conversation state for one task."""

    def decide(self, frame: PublicFrame, instruction: LocalInstruction) -> dict | ExecutorNoOp:
        """Return one normalized action, or an explicit native no-op."""

    def validate_action(self, action: dict, width: int, height: int,
                        *, allow_done: bool = True) -> dict:
        """Optionally validate one normalized public action."""

    @property
    def descriptor(self) -> Mapping[str, Any]:
        """Optionally return JSON-safe model and protocol metadata."""


class ExecutorContractError(TypeError):
    """A GUI Agent does not satisfy the recovery Executor contract."""


def validate_executor(executor: object) -> ExecutorProtocol:
    """Validate an Executor at the integration boundary.

    Only ``start_task`` and ``decide`` are mandatory. Optional capabilities are
    validated when present so a malformed custom adapter fails before a device
    trial starts rather than becoming an opaque ``agent_error``.
    """
    if executor is None:
        raise ExecutorContractError("Executor must be an object")
    for name in ("start_task", "decide"):
        if not callable(getattr(executor, name, None)):
            raise ExecutorContractError(f"Executor must provide callable {name}")
    validator = getattr(executor, "validate_action", None)
    if validator is not None and not callable(validator):
        raise ExecutorContractError("Executor validate_action must be callable")
    requires = getattr(executor, "requires_completion_check", False)
    if type(requires) is not bool:
        raise ExecutorContractError("Executor requires_completion_check must be boolean")
    descriptor = getattr(executor, "descriptor", None)
    if descriptor is not None:
        if not isinstance(descriptor, Mapping):
            raise ExecutorContractError("Executor descriptor must be a mapping")
        try:
            json.dumps(dict(descriptor), ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError):
            raise ExecutorContractError("Executor descriptor must be JSON-serializable") from None
    return executor  # type narrowing for callers; no wrapper or state is added.
