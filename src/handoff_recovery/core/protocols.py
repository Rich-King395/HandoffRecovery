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


HISTORY_POLICY_VERSION = "recovery_history_v1"
HISTORY_POLICY = {
    "version": HISTORY_POLICY_VERSION,
    "original": "retain_original_scope_history",
    "recovery": "start_each_recovery_scope_with_empty_native_history",
    "fallback": "restore_original_scope_history",
    "handoff_context": "structured_recovery_context_outside_native_history",
}

GUI_AGENT_ERROR_CATEGORIES = frozenset({
    "request_error", "response_error", "native_parse_error", "action_mapping_error",
    "history_error", "prompt_error", "completion_check_error", "executor_contract_error",
    "integration_error",
})


def classify_error(category: str | None, *, source: str) -> str:
    if source == "planner" or category in {"planner_error", "plan_validation_error",
                                            "planner_unavailable_or_invalid"}:
        return "recovery_control_error"
    if source == "device":
        return "device_error"
    if category in GUI_AGENT_ERROR_CATEGORIES or source == "executor":
        return "gui_agent_error"
    return "recovery_control_error"


def apply_history_policy(contexts, current_history, previous_scope, instruction):
    """Apply the one native-history policy for every recovery Executor.

    Recovery scopes deliberately start with an empty native conversation. A
    fallback resumes the original native history; Planner progress and
    recovery instructions remain separate structured context.
    """
    effective_scope = "original" if instruction.resume_original else instruction.scope
    if effective_scope == previous_scope:
        return current_history, previous_scope
    if previous_scope is not None:
        contexts[previous_scope] = deepcopy(current_history)
    history = (deepcopy(contexts.get("original", []))
               if instruction.resume_original else [])
    return history, effective_scope


def history_policy_descriptor():
    return dict(HISTORY_POLICY)


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
