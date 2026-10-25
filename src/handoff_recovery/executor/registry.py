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

from handoff_recovery.core.protocols import (DEFAULT_RECOVERY_HISTORY_MODE,
                                              RECOVERY_HISTORY_MODES,
                                              ExecutorProtocol, validate_executor)
from .cloud import make_cloud_executor
from .gui_owl import GUIOwlConfiguration, GUIOwlExecutor
from .holo import HoloConfiguration, HoloExecutor
from .phonebuddy import PhoneBuddyConfiguration, PhoneBuddyExecutor
from .ui_voyager import UIVoyagerConfiguration, UIVoyagerExecutor
from .ui_venus import UIVenusConfiguration, UIVenusExecutor
from .openmobile import OpenMobileConfiguration, OpenMobileExecutor
from .mai_gym import MAIGymConfiguration, MAIGymExecutor
from handoff_recovery.integrations.handoffbench import (
    load_gui_owl_bindings, load_phonebuddy_bindings, load_ui_voyager_bindings,
    load_holo_bindings, load_ui_venus_bindings, load_openmobile_bindings, load_mai_gym_bindings,
)


@dataclass(frozen=True)
class ExecutorSpec:
    """Model-neutral settings passed to a local GUI Agent adapter."""

    agent: str
    model: str
    base_url: str = ""
    api_key: str = "EMPTY"
    prompt_path: Path | None = None
    # Completion checks are opt-in; experiments currently use model-owned done.
    enable_completion_check: bool = False
    include_recovery_constraints: bool = False
    include_recovery_uncertain: bool = False
    timeout_seconds: float = 120.0
    total_timeout_seconds: float | None = 120.0
    proxy_mode: str = "environment"
    max_tokens: int = 2048
    configuration_source: str = "explicit_executor_spec"
    recovery_history_mode: str = DEFAULT_RECOVERY_HISTORY_MODE

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
        if type(self.include_recovery_constraints) is not bool:
            raise TypeError("Executor include_recovery_constraints must be boolean")
        if type(self.include_recovery_uncertain) is not bool:
            raise TypeError("Executor include_recovery_uncertain must be boolean")
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
        if not isinstance(self.configuration_source, str) or not self.configuration_source.strip():
            raise ValueError("Executor configuration_source must be nonempty text")
        if self.recovery_history_mode not in RECOVERY_HISTORY_MODES:
            raise ValueError("Executor recovery_history_mode must be isolated or original_snapshot")


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
    if (spec.strip().lower() in {"gui_owl", "phonebuddy", "ui_voyager", "holo", "ui_venus", "openmobile", "mai_gym"}
            and configuration.agent != spec.strip().lower()):
        raise ValueError("Executor adapter and spec agent must match")
    return validate_executor(_load_factory(spec)(bench_path, configuration))


def executor_adapter_names() -> tuple[str, ...]:
    return tuple(sorted(_FACTORIES))


def load_local_configuration(bench_path: str | Path, agent: str):
    """Read exactly the configuration used by the selected native baseline."""
    loaders = {"gui_owl": load_gui_owl_bindings, "phonebuddy": load_phonebuddy_bindings,
               "ui_voyager": load_ui_voyager_bindings, "holo": load_holo_bindings,
               "ui_venus": load_ui_venus_bindings, "openmobile": load_openmobile_bindings,
               "mai_gym": load_mai_gym_bindings}
    if agent not in loaders:
        raise ValueError("Unknown native Executor adapter")
    return loaders[agent](bench_path).validate_configuration()


def _local_configuration(bench_path: str | Path, spec: ExecutorSpec):
    if spec.include_recovery_constraints or spec.include_recovery_uncertain:
        raise ValueError("Native Executors do not support recovery constraints/uncertain injection")
    if spec.agent != "phonebuddy" and spec.enable_completion_check:
        raise ValueError("This native Executor does not support completion_check")
    if spec.configuration_source == "handoffbench_provider_validate_configuration":
        return load_local_configuration(bench_path, spec.agent)
    return spec


def _phonebuddy(bench_path: str | Path, spec: ExecutorSpec) -> ExecutorProtocol:
    bench = _local_configuration(bench_path, spec)
    configuration = PhoneBuddyConfiguration(
        api_key=bench.api_key, model=bench.model, base_url=bench.base_url,
        prompt_path=bench.prompt_path, enable_completion_check=spec.enable_completion_check,
        timeout_seconds=bench.timeout_seconds, max_tokens=bench.max_tokens,
        configuration_source=spec.configuration_source,
        recovery_history_mode=spec.recovery_history_mode,
        include_recovery_constraints=spec.include_recovery_constraints,
        include_recovery_uncertain=spec.include_recovery_uncertain)
    return PhoneBuddyExecutor(bench_path, configuration)


def _gui_owl(bench_path: str | Path, spec: ExecutorSpec) -> ExecutorProtocol:
    bench = _local_configuration(bench_path, spec)
    configuration = GUIOwlConfiguration(
        api_key=bench.api_key, model=bench.model, base_url=bench.base_url,
        timeout_seconds=bench.timeout_seconds, max_tokens=bench.max_tokens,
        configuration_source=spec.configuration_source,
        recovery_history_mode=spec.recovery_history_mode,
        include_recovery_constraints=spec.include_recovery_constraints,
        include_recovery_uncertain=spec.include_recovery_uncertain)
    return GUIOwlExecutor(bench_path, configuration)


def _cloud(provider: str):
    def factory(bench_path: str | Path, spec: ExecutorSpec) -> ExecutorProtocol:
        if spec.agent.strip().lower() != provider:
            # Keep the adapter and provider explicit when a caller uses a
            # custom ExecutorSpec assembled from a different model block.
            raise ValueError(f"Executor spec agent must be {provider}")
        return make_cloud_executor(bench_path, spec)
    return factory


def _ui_voyager(bench_path: str | Path, spec: ExecutorSpec) -> ExecutorProtocol:
    bench = _local_configuration(bench_path, spec)
    return UIVoyagerExecutor(bench_path, UIVoyagerConfiguration(
        api_key=bench.api_key, model=bench.model, base_url=bench.base_url,
        timeout_seconds=bench.timeout_seconds, max_tokens=bench.max_tokens,
        configuration_source=spec.configuration_source,
        recovery_history_mode=spec.recovery_history_mode))


def _holo(bench_path: str | Path, spec: ExecutorSpec) -> ExecutorProtocol:
    bench = _local_configuration(bench_path, spec)
    return HoloExecutor(bench_path, HoloConfiguration(
        api_key=bench.api_key, model=bench.model, base_url=bench.base_url,
        timeout_seconds=bench.timeout_seconds, max_tokens=bench.max_tokens,
        configuration_source=spec.configuration_source,
        recovery_history_mode=spec.recovery_history_mode))


def _ui_venus(bench_path: str | Path, spec: ExecutorSpec) -> ExecutorProtocol:
    bench = _local_configuration(bench_path, spec)
    return UIVenusExecutor(bench_path, UIVenusConfiguration(
        api_key=bench.api_key, model=bench.model, base_url=bench.base_url,
        timeout_seconds=bench.timeout_seconds, max_tokens=bench.max_tokens,
        configuration_source=spec.configuration_source,
        recovery_history_mode=spec.recovery_history_mode))


def _openmobile(bench_path: str | Path, spec: ExecutorSpec) -> ExecutorProtocol:
    bench = _local_configuration(bench_path, spec)
    return OpenMobileExecutor(bench_path, OpenMobileConfiguration(
        api_key=bench.api_key, model=bench.model, base_url=bench.base_url,
        timeout_seconds=bench.timeout_seconds, max_tokens=bench.max_tokens,
        configuration_source=spec.configuration_source,
        recovery_history_mode=spec.recovery_history_mode))


def _mai_gym(bench_path: str | Path, spec: ExecutorSpec) -> ExecutorProtocol:
    bench = _local_configuration(bench_path, spec)
    return MAIGymExecutor(bench_path, MAIGymConfiguration(
        api_key=bench.api_key, model=bench.model, base_url=bench.base_url,
        timeout_seconds=bench.timeout_seconds, max_tokens=bench.max_tokens,
        configuration_source=spec.configuration_source,
        recovery_history_mode=spec.recovery_history_mode,
        temperature=getattr(bench, "temperature", 0.0), top_p=getattr(bench, "top_p", 1.0)))


register_executor("phonebuddy", _phonebuddy)
register_executor("gui_owl", _gui_owl)
register_executor("qwen", _cloud("qwen"))
register_executor("deepseek", _cloud("deepseek"))
register_executor("openai", _cloud("openai"))
register_executor("claude", _cloud("claude"))
register_executor("qwen35", _cloud("qwen35"))
register_executor("lfm25_vl", _cloud("lfm25_vl"))
register_executor("ui_voyager", _ui_voyager)
register_executor("holo", _holo)


register_executor("ui_venus", _ui_venus)

register_executor("openmobile", _openmobile)

register_executor("mai_gym", _mai_gym)
