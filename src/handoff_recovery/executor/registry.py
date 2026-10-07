"""GUI Agent adapter registry.

Adapters receive a normalized ``ExecutorSpec`` and an explicit Benchmark
checkout path.  The recovery Coordinator sees only ``ExecutorProtocol``.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any

from handoff_recovery.core.protocols import ExecutorProtocol, validate_executor
from .cloud import make_cloud_executor
from .gui_owl import GUIOwlConfiguration, GUIOwlExecutor
from .holo import HoloConfiguration, HoloExecutor
from .phonebuddy import PhoneBuddyConfiguration, PhoneBuddyExecutor
from .ui_voyager import UIVoyagerConfiguration, UIVoyagerExecutor


@dataclass(frozen=True)
class ExecutorSpec:
    """Model-neutral settings passed to a local GUI Agent adapter."""

    agent: str
    model: str
    base_url: str = ""
    api_key: str = "EMPTY"
    prompt_path: Path | None = None
    enable_completion_check: bool = False
    timeout_seconds: float = 120.0
    total_timeout_seconds: float | None = 120.0
    proxy_mode: str = "environment"
    max_tokens: int = 2048

    def __post_init__(self):
        for value, name in ((self.agent, "agent"), (self.model, "model"),
                            (self.api_key, "api_key")):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"Executor {name} must be nonempty text")
        if not isinstance(self.base_url, str) or any(c.isspace() for c in self.base_url):
            raise ValueError("Executor base_url must be text without whitespace")
        if self.prompt_path is not None and not isinstance(self.prompt_path, Path):
            raise TypeError("Executor prompt_path must be a pathlib.Path or None")
        if type(self.enable_completion_check) is not bool:
            raise TypeError("Executor enable_completion_check must be boolean")
        for value, name in ((self.timeout_seconds, "timeout_seconds"),
                            (self.total_timeout_seconds, "total_timeout_seconds")):
            if value is None and name == "total_timeout_seconds":
                continue
            if type(value) not in (int, float) or value <= 0:
                raise ValueError(f"Executor {name} must be positive")
        if not isinstance(self.proxy_mode, str) or not self.proxy_mode.strip():
            raise ValueError("Executor proxy_mode must be nonempty text")
        if type(self.max_tokens) is not int or self.max_tokens <= 0:
            raise ValueError("Executor max_tokens must be positive")


ExecutorFactory = Callable[[str | Path, ExecutorSpec], ExecutorProtocol]
_FACTORIES: dict[str, ExecutorFactory] = {}


def register_executor(name: str, factory: ExecutorFactory, *, replace: bool = False) -> None:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Executor adapter name must be nonempty text")
    if not callable(factory):
        raise TypeError("Executor factory must be callable")
    key = name.strip().lower()
    if key in _FACTORIES and not replace:
        raise ValueError(f"Executor adapter already registered: {key}")
    _FACTORIES[key] = factory


def _load_factory(spec: str) -> ExecutorFactory:
    if ":" not in spec:
        try:
            return _FACTORIES[spec.strip().lower()]
        except KeyError:
            raise ValueError(f"Unknown Executor adapter: {spec}") from None
    module_name, attribute = spec.split(":", 1)
    if not module_name or not attribute or ":" in attribute:
        raise ValueError("Executor adapter path must use module:attribute")
    loaded: Any = getattr(import_module(module_name), attribute, None)
    if not callable(loaded):
        raise ValueError("Executor adapter factory is not callable")
    return loaded


def create_executor(spec: str, bench_path: str | Path, configuration: ExecutorSpec) -> ExecutorProtocol:
    if not isinstance(configuration, ExecutorSpec):
        raise TypeError("Executor configuration must be ExecutorSpec")
    return validate_executor(_load_factory(spec)(bench_path, configuration))


def executor_adapter_names() -> tuple[str, ...]:
    return tuple(sorted(_FACTORIES))


def _phonebuddy(bench_path: str | Path, spec: ExecutorSpec) -> ExecutorProtocol:
    return PhoneBuddyExecutor(bench_path, PhoneBuddyConfiguration(
        api_key=spec.api_key, model=spec.model, base_url=spec.base_url,
        prompt_path=spec.prompt_path, enable_completion_check=spec.enable_completion_check,
        timeout_seconds=spec.timeout_seconds, max_tokens=spec.max_tokens))


def _gui_owl(bench_path: str | Path, spec: ExecutorSpec) -> ExecutorProtocol:
    return GUIOwlExecutor(bench_path, GUIOwlConfiguration(
        api_key=spec.api_key, model=spec.model, base_url=spec.base_url,
        timeout_seconds=spec.timeout_seconds, max_tokens=spec.max_tokens))


def _cloud(provider: str):
    def factory(bench_path: str | Path, spec: ExecutorSpec) -> ExecutorProtocol:
        if spec.agent.strip().lower() != provider:
            # Keep the adapter and provider explicit when a caller uses a
            # custom ExecutorSpec assembled from a different model block.
            raise ValueError(f"Executor spec agent must be {provider}")
        return make_cloud_executor(bench_path, spec)
    return factory


def _ui_voyager(bench_path: str | Path, spec: ExecutorSpec) -> ExecutorProtocol:
    return UIVoyagerExecutor(bench_path, UIVoyagerConfiguration(
        api_key=spec.api_key, model=spec.model, base_url=spec.base_url,
        timeout_seconds=spec.timeout_seconds, max_tokens=spec.max_tokens))


def _holo(bench_path: str | Path, spec: ExecutorSpec) -> ExecutorProtocol:
    return HoloExecutor(bench_path, HoloConfiguration(
        api_key=spec.api_key, model=spec.model, base_url=spec.base_url,
        timeout_seconds=spec.timeout_seconds, max_tokens=spec.max_tokens))


register_executor("phonebuddy", _phonebuddy)
register_executor("gui_owl", _gui_owl)
register_executor("qwen", _cloud("qwen"))
register_executor("deepseek", _cloud("deepseek"))
register_executor("openai", _cloud("openai"))
register_executor("claude", _cloud("claude"))
register_executor("ui_voyager", _ui_voyager)
register_executor("holo", _holo)
