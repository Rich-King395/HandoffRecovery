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
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from handoff_recovery.core.types import LocalInstruction, PublicFrame
from handoff_recovery.integrations.handoffbench import load_cloud_bindings


class CloudExecutorError(RuntimeError):
    """One cloud GUI decision failed without an internal action retry."""

    def __init__(self, message: str, *, category: str, cause_type: str | None = None):
        super().__init__(message)
        self.category = category
        self.cause_type = cause_type


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

    def __post_init__(self) -> None:
        if self.provider.strip().lower() not in {"qwen", "deepseek", "openai", "claude"}:
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


def _current_context(instruction: LocalInstruction) -> str | None:
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
    if scope == "original" and not instruction.public_constraints:
        return None
    payload = {"scope": scope, "public_constraints": list(instruction.public_constraints)}
    if instruction.prior_progress:
        payload["prior_progress"] = list(instruction.prior_progress)
    if instruction.resume_original:
        payload["resume_original_context"] = True
        payload["resume_reason"] = instruction.resume_reason
    if scope == "verification":
        task = instruction.verification
        payload["verification"] = {
            "objective": task.objective,
            "required_evidence": list(task.required_evidence),
            "constraints": list(task.constraints),
        }
        guidance = (
            "Perform only the bounded verification task below. Native done or fail "
            "reports verification completion, not completion of the original task."
        )
    elif scope == "execution":
        handoff = instruction.handoff
        payload["handoff"] = {
            "remaining_objective": handoff.remaining_objective,
            "preserve": list(handoff.preserve),
            "completion_conditions": list(handoff.completion_conditions),
            "inspect_before_action": list(handoff.inspect_before_action),
            "conditional_actions": [{"when": item.when, "then": item.then}
                                    for item in handoff.conditional_actions],
        }
        guidance = (
            "Continue the original task using the remaining objective. The latest screenshot "
            "is authoritative; prior history is orientation only. Perform every listed "
            "inspect_before_action check before changing task data and resolve conditional "
            "actions locally. Before returning done, inspect the latest screen, check every "
            "completion_condition yourself, and preserve every listed constraint."
        )
    elif scope == "fallback":
        guidance = (
            "Resume the unchanged original task. The Planner did not provide reliable "
            "recovery guidance. The latest screenshot is authoritative; earlier history "
            "may be stale after takeover. Reassess the visible state and check the original "
            "task conditions yourself before returning done."
        )
    else:
        guidance = "Continue the unchanged original task with these public constraints."
    return guidance + "\n" + json.dumps(payload, ensure_ascii=False, allow_nan=False)


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

    def __init__(self, bench_path: str | Path, configuration: CloudExecutorConfiguration):
        if not isinstance(bench_path, (str, Path)) or not str(bench_path).strip():
            raise ValueError("An explicit Benchmark checkout path is required")
        if not isinstance(configuration, CloudExecutorConfiguration):
            raise TypeError("configuration must be CloudExecutorConfiguration")
        self._bench_path = bench_path
        self._configuration = configuration
        self._bindings = None
        self._task_id: str | None = None
        self._original_goal: str | None = None
        self._context: list[dict] = []
        self._context_scope: str | None = None
        self._contexts: dict[str, list[dict]] = {}

    @property
    def provider(self) -> str:
        return self._configuration.provider.strip().lower()

    @property
    def descriptor(self) -> dict:
        bindings = self._get_bindings()
        parsed = urlsplit(self._configuration.base_url)
        return {
            "adapter": self.provider,
            "model": self._configuration.model,
            "base_url_host": parsed.hostname or "native",
            "protocol": ("unique_json_action_v2+qwen_computer_use_xml"
                         if self.provider == "qwen" else "unique_json_action_v2"),
            "coordinate_space": bindings.coordinate_space,
            "recovery_scopes": ["original", "verification", "execution", "fallback"],
            "requires_completion_check": False,
            "history_scope": "current_task_only_retained_across_takeover",
            "transport": "handoffbench_api_method",
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
        context = _current_context(instruction)
        context_scope = "original" if instruction.resume_original else instruction.scope
        if context_scope != self._context_scope:
            if self._context_scope is not None:
                self._contexts[self._context_scope] = deepcopy(self._context)
            self._context = (deepcopy(self._contexts.get("original", []))
                             if instruction.resume_original else [])
            self._context_scope = context_scope
        bindings = self._get_bindings()
        api = bindings.api
        goal = self._original_goal if context is None else self._original_goal + "\n\n" + context
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
                configuration = self._provider_configuration(api)
                raw = api.request_action(
                    str(model_path), goal, *model_size, configuration=configuration,
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


def make_cloud_executor(bench_path: str | Path, spec) -> CloudExecutor:
    """Registry factory shared by the four provider names."""
    provider = spec.agent.strip().lower()
    total_timeout = None if provider == "qwen" else spec.total_timeout_seconds
    return CloudExecutor(bench_path, CloudExecutorConfiguration(
        provider=provider, api_key=spec.api_key, model=spec.model,
        base_url=spec.base_url, timeout_seconds=spec.timeout_seconds,
        total_timeout_seconds=total_timeout,
        proxy_mode=spec.proxy_mode, max_output_tokens=spec.max_tokens))
