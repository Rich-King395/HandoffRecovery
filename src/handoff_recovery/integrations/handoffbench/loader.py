"""Load public helpers from one explicitly selected Benchmark checkout."""
from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from importlib.machinery import ModuleSpec
from pathlib import Path
import sys
from threading import RLock
from types import ModuleType
from typing import Callable


class BenchmarkImportError(ValueError):
    """The explicit checkout is absent, incompatible, or conflicts with imports."""


@dataclass(frozen=True)
class GUIOwlBindings:
    api: ModuleType
    parse_tool_call: Callable
    map_mobile_use_action: Callable
    validate_action: Callable


@dataclass(frozen=True)
class PhoneBuddyBindings:
    api: ModuleType
    parse_tool_call: Callable
    map_phonebuddy_action: Callable
    history_entry: Callable


@dataclass(frozen=True)
class CloudBindings:
    """Public HandoffBench helpers for a shared cloud screenshot agent."""

    api: ModuleType
    parse_action: Callable
    map_action: Callable
    image_size: Callable
    validate_action: Callable
    coordinate_space: str
    provider: str


@dataclass(frozen=True)
class UIVoyagerBindings:
    api: ModuleType
    parse_tool_call: Callable
    map_mobile_use_action: Callable
    native_action_description: Callable
    validate_action: Callable


@dataclass(frozen=True)
class HoloBindings:
    api: ModuleType
    parse_completion: Callable
    map_holo_action: Callable
    validate_action: Callable


_IMPORT_LOCK = RLock()


def _is_contained_namespace(name: str, module: ModuleType, package_root: Path) -> bool:
    """Accept PEP 420 packages only when every search location is this checkout."""
    spec = getattr(module, "__spec__", None)
    if (not isinstance(spec, ModuleSpec) or spec.name != name or spec.origin is not None
            or spec.has_location or spec.submodule_search_locations is None):
        return False
    try:
        paths = tuple(Path(path).resolve() for path in module.__path__)
        locations = tuple(Path(path).resolve() for path in spec.submodule_search_locations)
        return bool(paths) and paths == locations and all(
            path.is_dir() and path.is_relative_to(package_root) for path in paths)
    except (AttributeError, TypeError, ValueError, OSError):
        return False


def _check_loaded_modules(package_root: Path) -> None:
    for name, module in tuple(sys.modules.items()):
        if name != "resumebench" and not name.startswith("resumebench."):
            continue
        if module is None:
            raise BenchmarkImportError(f"Conflicting loaded Benchmark module: {name}")
        filename = getattr(module, "__file__", None)
        if not filename:
            if not _is_contained_namespace(name, module, package_root):
                raise BenchmarkImportError(f"Loaded {name} does not belong to the explicit Benchmark path")
        elif not Path(filename).resolve().is_relative_to(package_root):
            raise BenchmarkImportError(f"Loaded {name} does not belong to the explicit Benchmark path")
        search_paths = getattr(module, "__path__", ())
        if any(not Path(path).resolve().is_relative_to(package_root) for path in search_paths):
            raise BenchmarkImportError(f"Loaded {name} has a foreign Benchmark package path")


def load_gui_owl_bindings(bench_path: str | Path) -> GUIOwlBindings:
    """Import without reading Benchmark configuration or discovering a checkout.

    ``bench_path`` is the directory containing ``resumebench``. Existing
    ``resumebench`` imports must all come from this same resolved checkout;
    foreign imports are rejected, never evicted or silently replaced.
    """
    if not isinstance(bench_path, (str, Path)) or not str(bench_path).strip():
        raise BenchmarkImportError("An explicit Benchmark checkout path is required")
    root = Path(bench_path).expanduser().resolve()
    package_root = root / "resumebench"
    required = (
        "__init__.py", "agents/gui_owl.py", "models/api_method_gui_owl.py",
        "devices/android.py",
    )
    if not root.is_dir() or any(
        not (package_root / part).is_file()
        or not (package_root / part).resolve().is_relative_to(package_root)
        for part in required
    ):
        raise BenchmarkImportError("Explicit Benchmark path lacks the public GUI-Owl modules")
    with _IMPORT_LOCK:
        _check_loaded_modules(package_root)
        sys.path.insert(0, str(root))
        try:
            api = import_module("resumebench.models.api_method_gui_owl")
            agent = import_module("resumebench.agents.gui_owl")
            android = import_module("resumebench.devices.android")
        except ImportError:
            raise BenchmarkImportError("Could not import the explicit Benchmark public helpers") from None
        finally:
            sys.path.remove(str(root))
        _check_loaded_modules(package_root)
    for module, names in (
        (api, ("system_prompt", "build_messages", "encode_screenshot")),
        (agent, ("parse_tool_call", "map_mobile_use_action")),
        (android, ("validate_action",)),
    ):
        if any(not callable(getattr(module, name, None)) for name in names):
            raise BenchmarkImportError("Benchmark GUI-Owl public helpers are incompatible")
    if getattr(api, "HISTORY_IMAGES", None) != 4:
        raise BenchmarkImportError("Benchmark GUI-Owl must retain four historical images")
    if any(not hasattr(api, name) for name in ("TEMPERATURE", "TOP_P", "TOP_K")):
        raise BenchmarkImportError("Benchmark GUI-Owl sampling parameters are unavailable")
    return GUIOwlBindings(api, agent.parse_tool_call, agent.map_mobile_use_action,
                          android.validate_action)


def load_phonebuddy_bindings(bench_path: str | Path) -> PhoneBuddyBindings:
    """Load PhoneBuddy's pinned public parser from one explicit checkout."""
    if not isinstance(bench_path, (str, Path)) or not str(bench_path).strip():
        raise BenchmarkImportError("An explicit Benchmark checkout path is required")
    root = Path(bench_path).expanduser().resolve()
    package_root = root / "resumebench"
    required = (
        "__init__.py", "agents/phonebuddy.py", "models/api_method_phonebuddy.py",
        "devices/android.py",
    )
    if not root.is_dir() or any(
        not (package_root / part).is_file()
        or not (package_root / part).resolve().is_relative_to(package_root)
        for part in required
    ):
        raise BenchmarkImportError("Explicit Benchmark path lacks the public PhoneBuddy modules")
    with _IMPORT_LOCK:
        _check_loaded_modules(package_root)
        sys.path.insert(0, str(root))
        try:
            api = import_module("resumebench.models.api_method_phonebuddy")
            agent = import_module("resumebench.agents.phonebuddy")
        except ImportError:
            raise BenchmarkImportError("Could not import the explicit Benchmark PhoneBuddy helpers") from None
        finally:
            sys.path.remove(str(root))
        _check_loaded_modules(package_root)
    for module, names in (
        (api, ("build_messages", "_openai_class", "_response_text")),
        (agent, ("parse_tool_call", "map_phonebuddy_action", "history_entry")),
    ):
        if any(not callable(getattr(module, name, None)) for name in names):
            raise BenchmarkImportError("Benchmark PhoneBuddy public helpers are incompatible")
    return PhoneBuddyBindings(api, agent.parse_tool_call, agent.map_phonebuddy_action,
                              agent.history_entry)


def load_cloud_bindings(bench_path: str | Path, provider: str) -> CloudBindings:
    """Load one of HandoffBench's shared visual cloud Agent bindings.

    The provider modules own transport and native response extraction. The
    recovery adapter only reuses their public JSON parser and coordinate map.
    """
    if not isinstance(bench_path, (str, Path)) or not str(bench_path).strip():
        raise BenchmarkImportError("An explicit Benchmark checkout path is required")
    if not isinstance(provider, str) or provider.strip().lower() not in {
            "qwen", "deepseek", "openai", "claude"}:
        raise BenchmarkImportError("Unsupported shared cloud provider")
    provider = provider.strip().lower()
    root = Path(bench_path).expanduser().resolve()
    package_root = root / "resumebench"
    required = (
        "__init__.py", "agents/cloud.py", f"agents/{provider}.py",
        f"models/api_method_{provider}.py", "models/gui_policy.py",
        "models/cloud_common.py", "devices/android.py",
    )
    if not root.is_dir() or any(
        not (package_root / part).is_file()
        or not (package_root / part).resolve().is_relative_to(package_root)
        for part in required
    ):
        raise BenchmarkImportError("Explicit Benchmark path lacks the public cloud Agent modules")
    with _IMPORT_LOCK:
        _check_loaded_modules(package_root)
        sys.path.insert(0, str(root))
        try:
            api = import_module(f"resumebench.models.api_method_{provider}")
            common = import_module("resumebench.agents.cloud")
            agent = import_module(f"resumebench.agents.{provider}")
        except ImportError:
            raise BenchmarkImportError("Could not import the explicit Benchmark cloud helpers") from None
        finally:
            sys.path.remove(str(root))
        _check_loaded_modules(package_root)
    for module, names in (
        (api, ("request_action",)),
        (common, ("parse_action", "map_image_pixel_action", "model_image_size")),
    ):
        if any(not callable(getattr(module, name, None)) for name in names):
            raise BenchmarkImportError("Benchmark cloud helpers are incompatible")
    if provider == "qwen":
        mapper = getattr(agent, "map_normalized_action", None)
        image_size = getattr(agent, "model_image_size", None)
        coordinate_space = "normalized_1000"
    elif provider == "deepseek":
        mapper = getattr(common, "map_image_pixel_action", None)
        image_size = getattr(agent, "model_image_size", None)
        coordinate_space = "image_pixels"
    else:
        mapper = getattr(common, "map_image_pixel_action", None)
        image_size = getattr(common, "model_image_size", None)
        coordinate_space = "image_pixels"
    if not callable(mapper) or not callable(image_size):
        raise BenchmarkImportError("Benchmark cloud coordinate helpers are incompatible")
    android = import_module("resumebench.devices.android")
    validator = getattr(android, "validate_action", None)
    if not callable(validator):
        raise BenchmarkImportError("Benchmark cloud action validator is incompatible")
    return CloudBindings(api, common.parse_action, mapper, image_size, validator,
                         coordinate_space, provider)


def load_ui_voyager_bindings(bench_path: str | Path) -> UIVoyagerBindings:
    """Load UI-Voyager's native tool parser and normalized action mapper."""
    root, package_root = _validate_bench_root(bench_path, (
        "__init__.py", "agents/ui_voyager.py", "models/api_method_ui_voyager.py",
        "devices/android.py",
    ), "UI-Voyager")
    with _IMPORT_LOCK:
        _check_loaded_modules(package_root)
        sys.path.insert(0, str(root))
        try:
            api = import_module("resumebench.models.api_method_ui_voyager")
            agent = import_module("resumebench.agents.ui_voyager")
            android = import_module("resumebench.devices.android")
        except ImportError:
            raise BenchmarkImportError("Could not import the explicit UI-Voyager helpers") from None
        finally:
            sys.path.remove(str(root))
        _check_loaded_modules(package_root)
    for module, names in (
        (api, ("request_action",)),
        (agent, ("parse_tool_call", "map_mobile_use_action", "native_action_description")),
        (android, ("validate_action",)),
    ):
        if any(not callable(getattr(module, name, None)) for name in names):
            raise BenchmarkImportError("Benchmark UI-Voyager helpers are incompatible")
    return UIVoyagerBindings(api, agent.parse_tool_call, agent.map_mobile_use_action,
                             agent.native_action_description, android.validate_action)


def load_holo_bindings(bench_path: str | Path) -> HoloBindings:
    """Load Holo's native completion parser and normalized action mapper."""
    root, package_root = _validate_bench_root(bench_path, (
        "__init__.py", "agents/holo.py", "models/api_method_holo.py",
        "devices/android.py",
    ), "Holo")
    with _IMPORT_LOCK:
        _check_loaded_modules(package_root)
        sys.path.insert(0, str(root))
        try:
            api = import_module("resumebench.models.api_method_holo")
            agent = import_module("resumebench.agents.holo")
            android = import_module("resumebench.devices.android")
        except ImportError:
            raise BenchmarkImportError("Could not import the explicit Holo helpers") from None
        finally:
            sys.path.remove(str(root))
        _check_loaded_modules(package_root)
    for module, names in (
        (api, ("request_action", "encode_screenshot")),
        (agent, ("parse_completion", "map_holo_action")),
        (android, ("validate_action",)),
    ):
        if any(not callable(getattr(module, name, None)) for name in names):
            raise BenchmarkImportError("Benchmark Holo helpers are incompatible")
    return HoloBindings(api, agent.parse_completion, agent.map_holo_action,
                        android.validate_action)


def _validate_bench_root(bench_path: str | Path, required: tuple[str, ...], label: str):
    if not isinstance(bench_path, (str, Path)) or not str(bench_path).strip():
        raise BenchmarkImportError("An explicit Benchmark checkout path is required")
    root = Path(bench_path).expanduser().resolve()
    package_root = root / "resumebench"
    if not root.is_dir() or any(
        not (package_root / part).is_file()
        or not (package_root / part).resolve().is_relative_to(package_root)
        for part in required
    ):
        raise BenchmarkImportError(f"Explicit Benchmark path lacks the public {label} modules")
    return root, package_root
