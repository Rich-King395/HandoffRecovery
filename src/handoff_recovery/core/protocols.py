"""Small extension points for model-specific adapters.

Concrete Planner and GUI Agent implementations may use any transport or
native protocol as long as they expose these normalized methods.
"""
from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any, Protocol, runtime_checkable

from .types import LocalInstruction, PlanReply, PlanRequest, PublicFrame


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
