"""Recovery Executor for HandoffBench's shared screenshot cloud agents.

The provider modules remain the source of truth for transport and response
parsing. This adapter only adds scoped recovery instructions and converts the
provider action into HandoffRecovery's normalized public action contract.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, fields
import json
import math
import os
from pathlib import Path
import re
import shutil
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from handoff_recovery.core.types import LocalInstruction, PublicFrame
from handoff_recovery.core.protocols import (RecoveryContext, apply_history_policy,
                                              DEFAULT_RECOVERY_HISTORY_MODE,
                                              RECOVERY_HISTORY_MODES,
                                              history_policy_descriptor, recovery_task_goal)
from handoff_recovery.integrations.handoffbench import load_cloud_bindings


class CloudExecutorError(RuntimeError):
    """One cloud GUI decision failed without an internal action retry."""

    def __init__(self, message: str, *, category: str, cause_type: str | None = None,
                 recoverable: bool = False):
        super().__init__(message)
        self.category = category
        self.cause_type = cause_type
        self.recoverable = recoverable


@dataclass(frozen=True)
class CloudExecutorConfiguration:
    """Provider-neutral settings for a HandoffBench cloud Agent."""

    provider: str
    api_key: str
    model: str
    base_url: str = ""
    timeout_seconds: float = 120.0
    total_timeout_seconds: float | None = 120.0
    proxy_mode: str = "environment"
    max_output_tokens: int = 2048
    # Completion checks are opt-in; keep the baseline recovery path unchanged.
    enable_completion_check: bool = False
    recovery_history_mode: str = DEFAULT_RECOVERY_HISTORY_MODE
    include_recovery_constraints: bool = False
    include_recovery_uncertain: bool = False

    def __post_init__(self) -> None:
        if self.provider.strip().lower() not in {"qwen", "deepseek", "openai", "claude", "qwen35", "lfm25_vl"}:
            raise ValueError("Unsupported cloud Executor provider")
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            raise ValueError("Cloud Executor api_key must be nonempty")
        for value, name in ((self.model, "model"),):
            if not isinstance(value, str) or not value.strip() or any(c.isspace() for c in value):
                raise ValueError(f"Cloud Executor {name} must be an identifier without whitespace")
        if not isinstance(self.base_url, str) or any(c.isspace() for c in self.base_url):
            raise ValueError("Cloud Executor base_url must be text without whitespace")
        for value, name in ((self.timeout_seconds, "timeout_seconds"),
                            (self.total_timeout_seconds, "total_timeout_seconds")):
            if value is None and name == "total_timeout_seconds":
                continue
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"Cloud Executor {name} must be finite and positive")
        if type(self.max_output_tokens) is not int or self.max_output_tokens <= 0:
            raise ValueError("Cloud Executor max_output_tokens must be positive")
        if type(self.enable_completion_check) is not bool:
            raise TypeError("Cloud Executor enable_completion_check must be boolean")
        if self.recovery_history_mode not in RECOVERY_HISTORY_MODES:
            raise ValueError("Cloud Executor recovery_history_mode must be isolated or original_snapshot")
        if type(self.include_recovery_constraints) is not bool:
            raise TypeError("Cloud Executor include_recovery_constraints must be boolean")
        if type(self.include_recovery_uncertain) is not bool:
            raise TypeError("Cloud Executor include_recovery_uncertain must be boolean")


def _current_context(instruction: LocalInstruction, *, include_constraints: bool = False,
                     include_uncertain: bool = False,
                     include_completion_check: bool = False) -> RecoveryContext | None:
    scope = instruction.scope
    if scope not in {"original", "verification", "execution", "fallback"}:
        raise ValueError("Unknown local instruction scope")
    if scope == "verification":
        if instruction.verification is None or instruction.handoff is not None:
            raise ValueError("Verification scope requires only a verification task")
    elif scope == "execution":
        if instruction.handoff is None or instruction.verification is not None:
            raise ValueError("Execution scope requires only a recovery handoff")
    elif instruction.verification is not None or instruction.handoff is not None:
        raise ValueError("Original and fallback scopes cannot contain recovery tasks")
    # Public constraints remain Planner/Coordinator metadata during the
    # original phase. Inject them only as recovery context after takeover.
    if scope in {"original", "fallback"}:
        return None
    payload = {"scope": scope,
               "condition_status": [
                   {"condition": condition, "status": status}
                   for condition, status in instruction.condition_status
               ],
               "last_action_feedback": (deepcopy(instruction.last_action_feedback)
                                         if instruction.last_action_feedback is not None else None)}
    if scope == "execution" and include_completion_check:
        payload["completion_check_required"] = True
    if scope == "verification":
        task = instruction.verification
        payload["verification"] = {
            "objective": task.objective,
            "required_evidence": list(task.required_evidence),
            "final_confirmation": task.final_confirmation,
        }
    elif scope == "execution":
        handoff = instruction.handoff
        payload["handoff"] = {
            "remaining_objective": handoff.remaining_objective,
            "completion_conditions": list(handoff.completion_conditions),
        }
    if instruction.completion_policy == "native":
        payload["completion_policy"] = "native"
    if instruction.condition_updates_enabled is not None:
        payload["condition_updates_enabled"] = instruction.condition_updates_enabled
    return payload


def _parse_qwen_computer_use(raw_text: str) -> dict:
    """Parse the XML tool-call variant emitted by some Qwen deployments.

    The shared HandoffBench cloud contract is JSON, but compatible Qwen
    endpoints may return ``computer_use`` in the checkpoint's native XML
    envelope even when the prompt requests a JSON action.  This parser only
    accepts one complete call, known parameters, and the same normalized
    action vocabulary used by the JSON parser; it does not repair actions.
    """
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise ValueError("Completion did not contain a Qwen computer_use call")
    match = re.fullmatch(
        r"\s*<tool_call>\s*<function=computer_use>\s*(.*?)\s*"
        r"</function>\s*</tool_call>\s*", raw_text, re.DOTALL,
    )
    if match is None:
        raise ValueError("Completion is not one complete Qwen computer_use call")
    body = match.group(1)
    parameters: dict[str, object] = {}
    cursor = 0
    pattern = re.compile(
        r"<parameter=([A-Za-z_][A-Za-z0-9_]*)>(.*?)</parameter>", re.DOTALL,
    )
    for item in pattern.finditer(body):
        if body[cursor:item.start()].strip():
            raise ValueError("Unexpected content between Qwen parameters")
        name, value = item.groups()
        if name in parameters:
            raise ValueError("Duplicate Qwen computer_use parameter")
        value = value.strip()
        if name in {"coords", "duration_ms", "seconds", "hold_ms"}:
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                raise ValueError(f"Qwen parameter {name} must contain JSON") from None
        parameters[name] = value
        cursor = item.end()
    if body[cursor:].strip():
        raise ValueError("Malformed Qwen computer_use parameters")
    action_name = parameters.pop("action", None)
    if not isinstance(action_name, str):
        raise ValueError("Qwen computer_use requires an action parameter")
    aliases = {"click": "tap", "type": "text", "scroll": "swipe"}
    action_name = aliases.get(action_name, action_name)
    if action_name == "text" and "input_text" not in parameters and "text" in parameters:
        parameters["input_text"] = parameters.pop("text")
    allowed = {
        "tap": {"coords"}, "text": {"coords", "input_text"},
        "swipe": {"coords", "duration_ms"}, "back": {"coords"},
        "home": {"coords"}, "wait": {"coords", "seconds"},
        "done": {"coords"}, "long_press": {"coords", "duration_ms"},
        "drag": {"coords", "duration_ms", "hold_ms"},
    }
    if action_name not in allowed:
        raise ValueError("Unsupported Qwen computer_use action")
    if set(parameters) - allowed[action_name] - {"target", "thought"}:
        raise ValueError("Unexpected Qwen computer_use parameters")
    if action_name in {"tap", "text", "swipe", "long_press", "drag"} and "coords" not in parameters:
        raise ValueError(f"Qwen {action_name} requires coords")
    if action_name == "text" and not isinstance(parameters.get("input_text"), str):
        raise ValueError("Qwen text action requires input_text")
    result = {"action": action_name, **parameters}
    return result


class CloudExecutor:
    """Provider-independent state machine for shared cloud screenshot agents."""

    requires_completion_check = False

    def __init__(self, bench_path: str | Path, configuration: CloudExecutorConfiguration,
                 *, allow_legacy_path: bool = False):
        if not isinstance(bench_path, (str, Path)) or not str(bench_path).strip():
            raise ValueError("An explicit Benchmark checkout path is required")
        if not isinstance(configuration, CloudExecutorConfiguration):
            raise TypeError("configuration must be CloudExecutorConfiguration")
        self._bench_path = bench_path
        self._configuration = configuration
        if type(allow_legacy_path) is not bool:
            raise TypeError("allow_legacy_path must be boolean")
        self._allow_legacy_path = allow_legacy_path
        self._bindings = None
        self._task_id: str | None = None
        self._original_goal: str | None = None
        self._context: list[dict] = []
        self._context_scope: str | None = None
        self._contexts: dict[str, list[dict]] = {}
        self._shared_agent = None
        self._audit_counter = 0
        self.requires_completion_check = configuration.enable_completion_check

    @property
    def provider(self) -> str:
        return self._configuration.provider.strip().lower()

    @property
    def descriptor(self) -> dict:
        bindings = self._get_bindings()
        if hasattr(bindings, "agent_class"):
            configuration = bindings.validate_configuration()
            agent = bindings.agent_class()
            agent._configuration = configuration
            # Copy the authoritative HandoffBench descriptor. Recovery fields
            # describe the Coordinator layer and do not redefine GUI protocol.
            descriptor = dict(agent.descriptor)
            descriptor.update({
                "adapter": self.provider,
                "output_normalization": getattr(agent, "output_normalization", "none"),
                "recovery_scopes": ["original", "verification", "execution", "fallback"],
                "recovery_context": "optional_for_execution_verification_fallback_only",
                "history_policy": history_policy_descriptor(self._configuration.recovery_history_mode),
                "history_owner": "handoff_recovery_host_policy",
                "recovery_constraints_in_prompt": self._configuration.include_recovery_constraints,
                "recovery_uncertain_in_prompt": self._configuration.include_recovery_uncertain,
                "requires_completion_check": self.requires_completion_check,
                "configuration_source": "handoffbench_provider_validate_configuration",
                "transport": "handoffbench_shared_cloud_agent",
                "legacy_path_enabled": False,
                "budget_semantics": "shared_total_v1_gui_decisions_only",
            })
            return descriptor
        if not self._allow_legacy_path:
            raise CloudExecutorError(
                "Cannot describe a cloud Executor without the HandoffBench shared Agent binding",
                category="integration_error")
        parsed = urlsplit(self._configuration.base_url)
        return {
            "adapter": self.provider,
            "model": self._configuration.model,
            "base_url_host": parsed.hostname or "native",
            "protocol": "legacy_recovery_only",
            "coordinate_space": bindings.coordinate_space,
            "recovery_scopes": ["original", "verification", "execution", "fallback"],
            "recovery_context": "shared_structured_mapping_v1",
            "requires_completion_check": self.requires_completion_check,
            "history_policy": history_policy_descriptor(self._configuration.recovery_history_mode),
            "history_owner": "handoff_recovery_host_policy",
            "transport": "legacy_recovery_cloud_executor",
            "configuration_source": "executor_spec_legacy",
            "legacy_path_enabled": True,
        }

    def _get_bindings(self):
        if self._bindings is None:
            try:
                self._bindings = load_cloud_bindings(self._bench_path, self.provider)
            except Exception as exc:
                raise CloudExecutorError(
                    "HandoffBench cloud integration is unavailable",
                    category="integration_error", cause_type=type(exc).__name__) from None
        return self._bindings

    def start_task(self, task_id: str, original_goal: str) -> None:
        if any(not isinstance(value, str) or not value.strip()
               for value in (task_id, original_goal)):
            raise ValueError("task_id and original_goal must be nonempty strings")
        self._task_id = task_id
        self._original_goal = original_goal
        self._context = []
        self._context_scope = None
        self._contexts = {"original": []}
        self._shared_agent = None
        self._audit_counter = 0

    def _persist_prompt_audit(self, temporary_directory: Path, scope: str) -> None:
        """Persist provider prompt audits when explicitly requested by a smoke run.

        HandoffBench writes the exact prompt and response audit beside the
        temporary model image.  The normal adapter keeps that directory
        ephemeral; this opt-in copy makes live integration tests inspectable
        without changing formal-run retention behavior.
        """
        target_root = os.environ.get("HANDOFF_RECOVERY_PROMPT_AUDIT_DIR", "").strip()
        if not target_root:
            return
        self._audit_counter += 1
        target = Path(target_root).expanduser().resolve() / (
            f"decision_{self._audit_counter:03d}_{scope}")
        target.mkdir(parents=True, exist_ok=True)
        for source in sorted(temporary_directory.glob("*_coordinates.json")):
            shutil.copy2(source, target / source.name)
        for source in sorted(temporary_directory.glob("*_transport.json")):
            shutil.copy2(source, target / source.name)

    def validate_action(self, action, width, height, *, allow_done=True):
        return self._get_bindings().validate_action(action, width, height, allow_done=allow_done)

    def _provider_configuration(self, api):
        classes = [value for name, value in vars(api).items()
                   if name.endswith("Configuration") and isinstance(value, type)]
        if len(classes) != 1:
            raise CloudExecutorError("Cloud API configuration class is unavailable",
                                     category="integration_error")
        cls = classes[0]
        available = {field.name for field in fields(cls)}
        source = {
            "api_key": self._configuration.api_key,
            "model": self._configuration.model,
            "base_url": self._configuration.base_url,
            "timeout_seconds": self._configuration.timeout_seconds,
            "total_timeout_seconds": self._configuration.total_timeout_seconds,
            "proxy_mode": self._configuration.proxy_mode,
            "max_output_tokens": self._configuration.max_output_tokens,
        }
        return cls(**{key: value for key, value in source.items() if key in available})

    def decide(self, frame: PublicFrame, instruction: LocalInstruction) -> dict:
        if self._task_id is None:
            raise ValueError("Call start_task before deciding")
        if not isinstance(frame, PublicFrame) or not isinstance(instruction, LocalInstruction):
            raise TypeError("decide accepts only PublicFrame and LocalInstruction")
        if instruction.original_goal != self._original_goal:
            raise ValueError("The original goal cannot change within a task")
        context = _current_context(
            instruction,
            include_constraints=self._configuration.include_recovery_constraints,
            include_uncertain=self._configuration.include_recovery_uncertain,
            include_completion_check=self.requires_completion_check,
        )
        self._context, self._context_scope = apply_history_policy(
            self._contexts, self._context, self._context_scope, instruction,
            recovery_history_mode=self._configuration.recovery_history_mode)
        bindings = self._get_bindings()
        if not hasattr(bindings, "agent_class"):
            if not self._allow_legacy_path:
                raise CloudExecutorError(
                    "HandoffBench shared cloud Agent binding is required; legacy CloudExecutor "
                    "path is disabled for formal experiments",
                    category="integration_error")
            return self._decide_with_legacy_policy(frame, instruction, context, bindings)
        return self._decide_with_shared_policy(frame, instruction, context, bindings)
    def _decide_with_legacy_policy(self, frame, instruction, context, bindings):
        """Compatibility-only path for migration tests; never used by the registry."""
        api = bindings.api
        goal = recovery_task_goal(self._original_goal, context)
        try:
            from PIL import Image
        except ImportError:
            raise CloudExecutorError("Cloud Executor requires Pillow",
                                     category="integration_error", cause_type="ImportError") from None
        try:
            with TemporaryDirectory(prefix="handoff-recovery-cloud-") as directory:
                path = Path(directory) / "public.png"
                model_path = Path(directory) / "model.png"
                path.write_bytes(frame.png)
                with Image.open(path) as image:
                    if image.size != (frame.width, frame.height):
                        raise ValueError("Public screenshot dimensions differ from the frame")
                    model_size = bindings.image_size(frame.width, frame.height)
                    resampling = getattr(Image, "Resampling", Image)
                    image.convert("RGB").resize(model_size, resampling.LANCZOS).save(model_path, "PNG")
                raw = api.request_action(
                    str(model_path), goal, *model_size,
                    configuration=self._provider_configuration(api),
                    history=deepcopy(self._context), allow_long_press=True, allow_drag=True,
                )
        except CloudExecutorError:
            raise
        except Exception as exc:
            raise CloudExecutorError(
                f"{self.provider} cloud request failed ({type(exc).__name__})",
                category="request_error", cause_type=type(exc).__name__) from None
        try:
            parsed = bindings.parse_action(raw)
        except Exception as exc:
            if self.provider == "qwen":
                try:
                    parsed = _parse_qwen_computer_use(raw)
                except Exception:
                    raise CloudExecutorError(
                        "Cloud Agent returned an invalid action",
                        category="native_parse_error", cause_type=type(exc).__name__) from None
            else:
                raise CloudExecutorError(
                    "Cloud Agent returned invalid JSON action",
                    category="native_parse_error", cause_type=type(exc).__name__) from None
        try:
            original_size = (frame.width, frame.height)
            model_size = bindings.image_size(*original_size)
            coordinate_size = (1000, 1000) if bindings.coordinate_space == "normalized_1000" else model_size
            action = (bindings.map_action(parsed, original_size)
                      if bindings.coordinate_space == "normalized_1000"
                      else bindings.map_action(parsed, coordinate_size, original_size))
        except Exception as exc:
            raise CloudExecutorError(
                "Cloud Agent action could not be mapped",
                category="action_mapping_error", cause_type=type(exc).__name__) from None
        self._context.append(deepcopy(parsed))
        return action

    def _decide_with_shared_policy(self, frame, instruction, context, bindings):
        """Delegate request, parsing, mapping, and validation to HandoffBench."""
        try:
            from PIL import Image
        except ImportError:
            raise CloudExecutorError("Cloud Executor requires Pillow",
                                     category="integration_error", cause_type="ImportError") from None
        if self._shared_agent is None:
            self._shared_agent = bindings.agent_class()
            # HandoffBench owns provider defaults, .env loading, validation,
            # timeout/retry policy, and model selection for the shared path.
            self._shared_agent._configuration = bindings.validate_configuration()
        try:
            with TemporaryDirectory(prefix="handoff-recovery-cloud-") as directory:
                path = Path(directory) / "public.png"
                path.write_bytes(frame.png)
                with Image.open(path) as image:
                    if image.size != (frame.width, frame.height):
                        raise ValueError("Public screenshot dimensions differ from the frame")
                action = self._shared_agent.decide(
                    {"screenshot": str(path), "width": frame.width, "height": frame.height},
                    self._original_goal, deepcopy(self._context),
                    recovery_context=context,
                    include_recovery_constraints=self._configuration.include_recovery_constraints,
                    allow_condition_updates=(context is not None
                                              and context.get("scope") == "execution"
                                              and (instruction.condition_updates_enabled
                                                   if instruction.condition_updates_enabled is not None
                                                   else instruction.completion_policy == "conditions")),
                    allow_completion_check=(self.requires_completion_check
                                             and context is not None
                                             and context.get("scope") == "execution"),
                )
                self._persist_prompt_audit(Path(directory), instruction.scope)
        except CloudExecutorError:
            raise
        except Exception as exc:
            raise CloudExecutorError(
                f"{self.provider} cloud request failed ({type(exc).__name__})",
                category="request_error", cause_type=type(exc).__name__) from None
        if action is None:
            raise CloudExecutorError("Cloud Agent returned no valid action",
                                     category="native_parse_error",
                                     recoverable=self.provider in {"qwen35", "lfm25_vl"})
        self._context.append(deepcopy(action))
        return action

    @staticmethod
    def _context_payload(context: dict | None):
        """Compatibility alias for callers inspecting the old helper."""
        return deepcopy(context) if context is not None else None


def make_cloud_executor(bench_path: str | Path, spec) -> CloudExecutor:
    """Registry factory shared by general screenshot model providers."""
    provider = spec.agent.strip().lower()
    total_timeout = None if provider == "qwen" else spec.total_timeout_seconds
    return CloudExecutor(bench_path, CloudExecutorConfiguration(
        provider=provider, api_key=spec.api_key, model=spec.model,
        base_url=spec.base_url, timeout_seconds=spec.timeout_seconds,
        total_timeout_seconds=total_timeout,
        proxy_mode=spec.proxy_mode, max_output_tokens=spec.max_tokens,
        enable_completion_check=spec.enable_completion_check,
        recovery_history_mode=spec.recovery_history_mode,
        include_recovery_constraints=spec.include_recovery_constraints,
        include_recovery_uncertain=spec.include_recovery_uncertain))
