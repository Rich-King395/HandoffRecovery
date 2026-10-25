"""Recovery Executor for HandoffBench's Holo Agent."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, fields, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from handoff_recovery.core.types import LocalInstruction, PublicFrame
from handoff_recovery.core.protocols import ExecutorNoOp
from handoff_recovery.core.protocols import (DEFAULT_RECOVERY_HISTORY_MODE,
                                              RECOVERY_HISTORY_MODES,
                                              apply_history_policy, history_policy_descriptor,
                                              recovery_task_goal)
from handoff_recovery.integrations.handoffbench import load_holo_bindings
from .cloud import _current_context
from .condition_updates import PROTOCOL, FORMAT_SHA256, enabled, request_options, parse_native_response
from .config import validate_finite_positive, validate_http_url, validate_positive_int


class HoloExecutorError(RuntimeError):
    def __init__(self, message: str, *, category: str, cause_type: str | None = None, recoverable: bool = False):
        super().__init__(message)
        self.category = category
        self.cause_type = cause_type
        self.recoverable = recoverable


@dataclass(frozen=True)
class HoloConfiguration:
    api_key: str = "EMPTY"
    model: str = "Holo-3.1-4B"
    base_url: str = "http://127.0.0.1:8003/v1"
    timeout_seconds: float = 200.0
    max_tokens: int = 2048
    recovery_history_mode: str = DEFAULT_RECOVERY_HISTORY_MODE
    configuration_source: str = "explicit_executor_spec"

    def __post_init__(self):
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            raise ValueError("Holo api_key must be nonempty")
        if not isinstance(self.model, str) or not self.model.strip() or any(c.isspace() for c in self.model):
            raise ValueError("Holo model must be an identifier without whitespace")
        validate_http_url(self.base_url, "Holo base_url must be a plain HTTP(S) URL")
        validate_finite_positive(self.timeout_seconds, "Holo timeout_seconds must be positive")
        validate_positive_int(self.max_tokens, "Holo max_tokens must be positive")
        if self.recovery_history_mode not in RECOVERY_HISTORY_MODES:
            raise ValueError("Holo recovery_history_mode must be isolated or original_snapshot")
        if not isinstance(self.configuration_source, str) or not self.configuration_source.strip():
            raise ValueError("Holo configuration_source must be nonempty text")


class HoloExecutor:
    condition_updates_protocol = PROTOCOL
    requires_completion_check = False

    def __init__(self, bench_path: str | Path, configuration: HoloConfiguration):
        if not isinstance(configuration, HoloConfiguration):
            raise TypeError("configuration must be HoloConfiguration")
        self._bench_path = bench_path
        self._configuration = configuration
        self._bindings = None
        self._task_id = None
        self._original_goal = None
        self._context: list[dict] = []
        self._context_scope = None
        self._contexts: dict[str, list[dict]] = {}
        self._tempdir: TemporaryDirectory | None = None
        self._frame_number = 0

    def __del__(self):
        tempdir = getattr(self, "_tempdir", None)
        if tempdir is not None:
            try:
                tempdir.cleanup()
            except Exception:
                pass

    def _get_bindings(self):
        if self._bindings is None:
            try:
                self._bindings = load_holo_bindings(self._bench_path)
            except Exception as exc:
                raise HoloExecutorError(
                    "HandoffBench Holo integration is unavailable",
                    category="integration_error", cause_type=type(exc).__name__) from None
        return self._bindings

    @property
    def descriptor(self):
        return {
            "adapter": "holo", "model": self._configuration.model,
            "base_url_host": urlsplit(self._configuration.base_url).hostname or "",
            "protocol": "native_function_call_or_qwen_xml", "coordinate_space": "normalized_1000",
            "recovery_scopes": ["original", "verification", "execution", "fallback"],
            "history_scope": "current_scope_only_by_default",
            "requires_completion_check": False,
            "configuration_source": self._configuration.configuration_source,
            "timeout_seconds": self._configuration.timeout_seconds,
            "max_tokens": self._configuration.max_tokens,
            "history_policy": history_policy_descriptor(self._configuration.recovery_history_mode),
            "history_owner": "handoff_recovery_host_policy",
            "condition_updates_protocol": PROTOCOL,
            "condition_updates_format_sha256": FORMAT_SHA256,
            "budget_semantics": "shared_total_v1_gui_decisions_only",
        }

    def start_task(self, task_id: str, original_goal: str) -> None:
        if any(not isinstance(value, str) or not value.strip() for value in (task_id, original_goal)):
            raise ValueError("task_id and original_goal must be nonempty strings")
        if self._tempdir is not None:
            self._tempdir.cleanup()
        self._tempdir = TemporaryDirectory(prefix="handoff-recovery-holo-")
        self._frame_number = 0
        self._task_id, self._original_goal = task_id, original_goal
        self._context, self._context_scope, self._contexts = [], None, {"original": []}

    def validate_action(self, action, width, height, *, allow_done=True):
        return self._get_bindings().validate_action(action, width, height, allow_done=allow_done)

    def _configuration_for(self, api):
        classes = [value for name, value in vars(api).items()
                   if name.endswith("Configuration") and isinstance(value, type)]
        if len(classes) != 1:
            raise HoloExecutorError("Holo configuration class is unavailable", category="integration_error")
        cls = classes[0]
        available = {field.name for field in fields(cls)}
        values = {"api_key": self._configuration.api_key, "model": self._configuration.model,
                  "base_url": self._configuration.base_url,
                  "timeout_seconds": self._configuration.timeout_seconds,
                  "max_tokens": self._configuration.max_tokens}
        return cls(**{key: value for key, value in values.items() if key in available})

    def decide(self, frame: PublicFrame, instruction: LocalInstruction) -> dict:
        if self._task_id is None or self._tempdir is None:
            raise ValueError("Call start_task before deciding")
        if not isinstance(frame, PublicFrame) or not isinstance(instruction, LocalInstruction):
            raise TypeError("decide accepts only PublicFrame and LocalInstruction")
        if instruction.original_goal != self._original_goal:
            raise ValueError("The original goal cannot change within a task")
        context = _current_context(instruction)
        self._context, self._context_scope = apply_history_policy(
            self._contexts, self._context, self._context_scope, instruction,
            recovery_history_mode=self._configuration.recovery_history_mode)
        bindings = self._get_bindings()
        api = bindings.api
        self._frame_number += 1
        path = Path(self._tempdir.name) / f"public_{self._frame_number}.png"
        path.write_bytes(frame.png)
        goal = recovery_task_goal(self._original_goal, context)
        try:
            image = api.encode_screenshot(path, expected_size=(frame.width, frame.height))
            response = api.request_action(
                str(path), goal, configuration=self._configuration_for(api),
                history=deepcopy(self._context), current_image=image,
                **request_options(instruction),
            )
        except HoloExecutorError:
            raise
        except Exception as exc:
            raise HoloExecutorError(
                f"Holo request failed ({type(exc).__name__})",
                category="request_error", cause_type=type(exc).__name__) from None
        try:
            updates = None
            if enabled(instruction):
                parsed, _content, updates = parse_native_response(
                    response.content,
                    lambda content: bindings.parse_completion(replace(response, content=content)),
                    active=True)
            else:
                parsed = bindings.parse_completion(response)
        except Exception as exc:
            raise HoloExecutorError(
                "Holo returned an invalid native completion",
                category="native_parse_error", cause_type=type(exc).__name__,
                recoverable=isinstance(exc, (ValueError, OverflowError))) from None
        try:
            action = (bindings.map_holo_action(parsed.call, (frame.width, frame.height))
                      if parsed.call is not None else None)
            if action is not None:
                bindings.validate_action(action, frame.width, frame.height, allow_done=True)
            if parsed.reasoning:
                if action is not None:
                    action["thought"] = parsed.reasoning
            assistant = {"role": "assistant", "content": parsed.content}
            if parsed.call is not None:
                assistant["tool_calls"] = [{
                    "id": f"holo_call_{len(self._context) + 1}", "type": "function",
                    "function": deepcopy(parsed.call),
                }]
            history_entry = {"assistant": assistant, "image": image}
        except Exception as exc:
            raise HoloExecutorError(
                f"Holo action could not be mapped or validated: {str(exc)[:240]}",
                category="action_mapping_error", cause_type=type(exc).__name__,
                recoverable=isinstance(exc, (ValueError, OverflowError))) from None
        try:
            self._context.append(history_entry)
            history_images = getattr(api, "HISTORY_IMAGES", 2)
            self._context = [{
                "assistant": item["assistant"],
                "image": item["image"] if index >= len(self._context) - history_images else None,
            } for index, item in enumerate(self._context)]
            if action is not None and updates is not None:
                action["condition_updates"] = updates
            return action if action is not None else ExecutorNoOp()
        except Exception as exc:
            raise HoloExecutorError(
                "Holo native history could not be serialized",
                category="history_error", cause_type=type(exc).__name__) from None


def make_holo_executor(bench_path, spec):
    from .registry import create_executor
    return create_executor("holo", bench_path, spec)
