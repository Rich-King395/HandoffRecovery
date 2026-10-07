"""Planner adapter registry.

The coordinator depends only on :class:`PlannerProtocol`.  This module keeps
transport and provider selection at the integration boundary so adding a new
commercial Planner does not require changing recovery state-machine code.
"""
from __future__ import annotations

from collections.abc import Callable
from importlib import import_module
from typing import Any

from handoff_recovery.core.protocols import PlannerProtocol
from .client import OpenAICompatiblePlanner, PlannerConfig

PlannerFactory = Callable[[PlannerConfig], PlannerProtocol]
_FACTORIES: dict[str, PlannerFactory] = {}


def register_planner(name: str, factory: PlannerFactory, *, replace: bool = False) -> None:
    """Register a named Planner factory.

    ``factory`` receives the normalized explicit ``PlannerConfig``.  A custom
    provider may ignore fields it does not need, or translate them to its own
    SDK configuration.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Planner adapter name must be nonempty text")
    if not callable(factory):
        raise TypeError("Planner factory must be callable")
    key = name.strip().lower()
    if key in _FACTORIES and not replace:
        raise ValueError(f"Planner adapter already registered: {key}")
    _FACTORIES[key] = factory


def _load_factory(spec: str) -> PlannerFactory:
    if ":" not in spec:
        try:
            return _FACTORIES[spec.strip().lower()]
        except KeyError:
            raise ValueError(f"Unknown Planner adapter: {spec}") from None
    module_name, attribute = spec.split(":", 1)
    if not module_name or not attribute or ":" in attribute:
        raise ValueError("Planner adapter path must use module:attribute")
    loaded: Any = getattr(import_module(module_name), attribute, None)
    if not callable(loaded):
        raise ValueError("Planner adapter factory is not callable")
    return loaded


def create_planner(spec: str, config: PlannerConfig) -> PlannerProtocol:
    """Create a Planner from a built-in name or ``module:factory`` path."""
    if not isinstance(config, PlannerConfig):
        raise TypeError("Planner config must be PlannerConfig")
    return _load_factory(spec)(config)


def planner_adapter_names() -> tuple[str, ...]:
    return tuple(sorted(_FACTORIES))


register_planner("openai_compatible", OpenAICompatiblePlanner)
register_planner("openai", OpenAICompatiblePlanner)

