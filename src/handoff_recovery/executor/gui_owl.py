"""One-request GUI-Owl decisions with explicit, scoped recovery context.

Only ``start_task`` resets task state. The host history policy starts recovery
scopes empty by default and retains actions within the active scope; an
explicit A/B mode can seed a recovery scope from the original snapshot. The
coordinator counts every attempted decision and interprets done/fail in its
current scope; this adapter never executes a device action or retries.

The HandoffRecoveryAgent bridge connects this executor to Benchmark
``run_task`` through public control notifications and scoped completion.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable
from urllib.parse import urlsplit

from handoff_recovery.core.types import LocalInstruction, PublicFrame
from handoff_recovery.core.protocols import (RecoveryContext, apply_history_policy,
                                              DEFAULT_RECOVERY_HISTORY_MODE,
                                              RECOVERY_HISTORY_MODES,
                                              history_policy_descriptor, recovery_task_goal)
from handoff_recovery.integrations.handoffbench import load_gui_owl_bindings
from .config import validate_finite_positive, validate_http_url, validate_positive_int
from .condition_updates import PROTOCOL, FORMAT_SHA256, enabled, request_options, parse_native_response


class ExecutorError(RuntimeError):
    """One local decision failed; callers must still count that attempt."""

    def __init__(self, message: str, *, category: str = "request_error",
                 cause_type: str | None = None, recoverable: bool = False):
        super().__init__(message)
        self.category = category
        self.cause_type = cause_type
        self.recoverable = recoverable


@dataclass(frozen=True)
class GUIOwlConfiguration:
    """Explicit client settings; no environment or Benchmark .env is read."""

    api_key: str = field(default="EMPTY", repr=False)
    model: str = "GUI-Owl-1.5-4B-Instruct"
    base_url: str = "http://127.0.0.1:8002/v1"
    timeout_seconds: float = 180.0
    max_tokens: int = 2048
    configuration_source: str = "explicit_executor_spec"
    recovery_history_mode: str = DEFAULT_RECOVERY_HISTORY_MODE
    include_recovery_constraints: bool = False
    include_recovery_uncertain: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            raise ValueError("GUI-Owl api_key must be nonempty; use EMPTY for a local endpoint")
        if (not isinstance(self.model, str) or not self.model
                or any(char.isspace() for char in self.model)):
            raise ValueError("GUI-Owl model must be an identifier without whitespace")
        validate_http_url(
            self.base_url,
            "GUI-Owl base_url must be HTTP(S) without credentials, query or fragment",
        )
        validate_finite_positive(
            self.timeout_seconds,
            "GUI-Owl timeout_seconds must be finite and positive",
        )
        validate_positive_int(self.max_tokens, "GUI-Owl max_tokens must be a positive integer")
        if not isinstance(self.configuration_source, str) or not self.configuration_source.strip():
            raise ValueError("GUI-Owl configuration_source must be nonempty text")
        if self.recovery_history_mode not in RECOVERY_HISTORY_MODES:
            raise ValueError("GUI-Owl recovery_history_mode must be isolated or original_snapshot")
        if type(self.include_recovery_constraints) is not bool:
            raise TypeError("GUI-Owl include_recovery_constraints must be boolean")
        if type(self.include_recovery_uncertain) is not bool:
            raise TypeError("GUI-Owl include_recovery_uncertain must be boolean")


def _openai_class():
    try:
        from openai import OpenAI
    except ImportError:
        raise ExecutorError("GUI-Owl requires the optional openai dependency") from None
    return OpenAI


def _current_context(instruction: LocalInstruction, *, include_constraints: bool = False,
                     include_uncertain: bool = False) -> RecoveryContext | None:
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
    # Keep the pre-handoff GUI prompt identical to the baseline. Constraints
    # are injected only when a recovery scope is active.
    if scope in {"original", "fallback"}:
        return None
    payload = {"scope": scope}
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
    if instruction.completion_policy == "native" or instruction.condition_updates_enabled is not None:
        payload["condition_status"] = [
            {"condition": condition, "status": status}
            for condition, status in instruction.condition_status]
        payload["last_action_feedback"] = deepcopy(instruction.last_action_feedback)
    if instruction.completion_policy == "native":
        payload["completion_policy"] = "native"
    if instruction.condition_updates_enabled is not None:
        payload["condition_updates_enabled"] = instruction.condition_updates_enabled
    return payload


class GUIOwlExecutor:
    condition_updates_protocol = PROTOCOL
    def __init__(self, bench_path: str | Path, configuration: GUIOwlConfiguration,
                 *, client_factory: Callable | None = None):
        if not isinstance(bench_path, (str, Path)) or not str(bench_path).strip():
            raise ValueError("An explicit Benchmark checkout path is required")
        if not isinstance(configuration, GUIOwlConfiguration):
            raise TypeError("configuration must be GUIOwlConfiguration")
        self._bench_path = bench_path
        self._configuration = configuration
        self._client_factory = client_factory
        self._bindings = None
        self._task_id: str | None = None
        self._original_goal: str | None = None
        self._context: list[dict] = []
        self._context_scope: str | None = None
        self._contexts: dict[str, list[dict]] = {}

    @property
    def descriptor(self):
        """Stable, non-secret metadata for experiment manifests."""
        return {
            "adapter": "gui_owl",
            "model": self._configuration.model,
            "base_url_host": urlsplit(self._configuration.base_url).hostname or "",
            "protocol": "mobile_use",
            "native_protocol": "gui_owl_mobile_use_v1",
            "action_normalization": "native_parser_then_handoffbench_mapper",
            "request_entrypoint": "handoffbench.api.request_action",
            "configuration_source": self._configuration.configuration_source,
            "coordinate_space": "image_pixels",
            "native_coordinate_space": "normalized_1000",
            "coordinate_compatibility": "strict_handoffbench_native_normalized_1000",
            "recovery_scopes": ["original", "verification", "execution", "fallback"],
            "requires_completion_check": False,
            "recovery_constraints_in_prompt": self._configuration.include_recovery_constraints,
            "recovery_uncertain_in_prompt": self._configuration.include_recovery_uncertain,
            "history_policy": history_policy_descriptor(self._configuration.recovery_history_mode),
            "history_owner": "handoff_recovery_host_policy",
            "condition_updates_protocol": PROTOCOL,
            "condition_updates_format_sha256": FORMAT_SHA256,
            "budget_semantics": "shared_total_v1_gui_decisions_only",
        }

    def start_task(self, task_id: str, original_goal: str) -> None:
        """Begin a new task, even when it has the same text as its predecessor."""
        if any(not isinstance(value, str) or not value.strip()
               for value in (task_id, original_goal)):
            raise ValueError("task_id and original_goal must be nonempty strings")
        self._task_id = task_id
        self._original_goal = original_goal
        self._context = []
        self._context_scope = None
        self._contexts = {"original": []}

    def validate_action(self, action, width, height, *, allow_done=True):
        """Validate the normalized action using GUI-Owl's public adapter."""
        if self._bindings is None:
            try:
                self._bindings = load_gui_owl_bindings(self._bench_path)
            except Exception as exc:
                raise ExecutorError(
                    "GUI-Owl Benchmark integration is unavailable",
                    category="integration_error", cause_type=type(exc).__name__) from None
        return self._bindings.validate_action(action, width, height, allow_done=allow_done)

    def decide(self, frame: PublicFrame, instruction: LocalInstruction) -> dict:
        """Return exactly one mapped action, or raise without a retry or action."""
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
        )
        self._context, self._context_scope = apply_history_policy(
            self._contexts, self._context, self._context_scope, instruction,
            recovery_history_mode=self._configuration.recovery_history_mode)
        if self._bindings is None:
            try:
                self._bindings = load_gui_owl_bindings(self._bench_path)
            except Exception as exc:
                raise ExecutorError(
                    "GUI-Owl Benchmark integration is unavailable",
                    category="integration_error", cause_type=type(exc).__name__) from None
        bindings = self._bindings
        api = bindings.api
        size = (frame.width, frame.height)
        # Benchmark accepts paths; this private temporary PNG is written only
        # from immutable public bytes and deleted before the model is contacted.
        with TemporaryDirectory(prefix="handoff-recovery-frame-") as directory:
            path = Path(directory) / "public.png"
            path.write_bytes(frame.png)
            image = api.encode_screenshot(path, expected_size=size)
            request_goal = recovery_task_goal(self._original_goal, context)
            if self._client_factory is None and callable(getattr(bindings, "request_action", None)):
                # The production path is owned by HandoffBench. Keep the
                # temporary screenshot alive through the complete request.
                try:
                    raw = bindings.request_action(
                        path, self._original_goal, configuration=self._configuration,
                        history=list(self._context), current_image=image,
                        recovery_context=context)
                except Exception as exc:
                    raise ExecutorError(
                        f"GUI-Owl request failed ({type(exc).__name__})",
                        category="request_error", cause_type=type(exc).__name__) from None
            else:
                # Kept only for injected offline test clients.
                messages = api.build_messages(image, request_goal, list(self._context),
                                              **request_options(instruction))
                raw = self._request(messages)
        try:
            (call, target), raw, updates = parse_native_response(
                raw, bindings.parse_tool_call, active=enabled(instruction))
        except Exception as exc:
            raise ExecutorError("GUI-Owl returned an invalid native decision",
                                category="native_parse_error", cause_type=type(exc).__name__,
                                recoverable=isinstance(exc, (ValueError, OverflowError))) from None
        try:
            action = bindings.map_mobile_use_action(call, size)
            action["target"] = target
            bindings.validate_action(action, *size, allow_done=True)
        except Exception as exc:
            # Keep the category stable for metrics, but retain the public
            # mapper's non-sensitive reason for diagnosing protocol drift.
            reason = str(exc).strip()
            suffix = f": {reason[:240]}" if reason else ""
            raise ExecutorError(
                f"GUI-Owl action could not be mapped or validated{suffix}",
                category="action_mapping_error", cause_type=type(exc).__name__,
                recoverable=isinstance(exc, (ValueError, OverflowError))) from None
        history = self._context + [{"output": raw, "image": image}]
        self._context = [
            {"output": item["output"],
             "image": item["image"] if index >= len(history) - api.HISTORY_IMAGES else None}
            for index, item in enumerate(history)
        ]
        if updates is not None:
            action["condition_updates"] = updates
        return action

    def _request(self, messages: list[dict]) -> str:
        configuration = self._configuration
        factory = self._client_factory or _openai_class()
        api = self._bindings.api
        try:
            with factory(api_key=configuration.api_key, base_url=configuration.base_url,
                         timeout=configuration.timeout_seconds, max_retries=0) as client:
                response = client.chat.completions.create(
                    model=configuration.model, messages=messages, stream=False,
                    max_tokens=configuration.max_tokens, temperature=api.TEMPERATURE,
                    top_p=api.TOP_P, extra_body={"top_k": api.TOP_K},
                )
        except Exception as error:
            # SDK errors can contain credentials, request images or server text.
            # Report only the exception type and a validated HTTP status.
            status = getattr(error, "status_code", None)
            suffix = f", HTTP {status}" if type(status) is int and 100 <= status <= 599 else ""
            raise ExecutorError(f"GUI-Owl request failed ({type(error).__name__}{suffix})") from None
        try:
            message = response.choices[0].message
            content = message.content
            tool_calls = getattr(message, "tool_calls", None)
        except (AttributeError, IndexError, TypeError) as exc:
            raise ExecutorError("GUI-Owl response is missing its completion message",
                                category="response_error", cause_type=type(exc).__name__) from None
        if not isinstance(content, str) or not content.strip() or tool_calls:
            raise ExecutorError("GUI-Owl requires a nonempty native text completion",
                                category="response_error")
        return content
