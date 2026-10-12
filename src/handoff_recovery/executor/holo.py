"""Recovery Executor for HandoffBench's Holo Agent."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, fields
import math
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from handoff_recovery.core.types import LocalInstruction, PublicFrame
from handoff_recovery.core.protocols import ExecutorNoOp
from handoff_recovery.core.protocols import apply_history_policy, history_policy_descriptor
from handoff_recovery.integrations.handoffbench import load_holo_bindings
from .cloud import _current_context


class HoloExecutorError(RuntimeError):
    def __init__(self, message: str, *, category: str, cause_type: str | None = None):
        super().__init__(message)
        self.category = category
        self.cause_type = cause_type


@dataclass(frozen=True)
class HoloConfiguration:
    api_key: str = "EMPTY"
    model: str = "Holo-3.1-4B"
    base_url: str = "http://127.0.0.1:8003/v1"
    timeout_seconds: float = 200.0
    max_tokens: int = 2048

    def __post_init__(self):
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            raise ValueError("Holo api_key must be nonempty")
        if not isinstance(self.model, str) or not self.model.strip() or any(c.isspace() for c in self.model):
            raise ValueError("Holo model must be an identifier without whitespace")
        parsed = urlsplit(self.base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or any(c.isspace() for c in self.base_url)):
            raise ValueError("Holo base_url must be a plain HTTP(S) URL")
        if type(self.timeout_seconds) not in (int, float) or not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("Holo timeout_seconds must be positive")
        if type(self.max_tokens) is not int or self.max_tokens <= 0:
            raise ValueError("Holo max_tokens must be positive")


class HoloExecutor:
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
            "history_scope": "current_task_only_retained_across_takeover",
            "requires_completion_check": False,
            "history_policy": history_policy_descriptor(),
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
            self._contexts, self._context, self._context_scope, instruction)
        bindings = self._get_bindings()
        api = bindings.api
        self._frame_number += 1
        path = Path(self._tempdir.name) / f"public_{self._frame_number}.png"
        path.write_bytes(frame.png)
        goal = self._original_goal if context is None else self._original_goal + "\n\n" + context
        try:
            image = api.encode_screenshot(path, expected_size=(frame.width, frame.height))
            response = api.request_action(
                str(path), goal, configuration=self._configuration_for(api),
                history=deepcopy(self._context), current_image=image,
            )
        except HoloExecutorError:
            raise
        except Exception as exc:
            raise HoloExecutorError(
                f"Holo request failed ({type(exc).__name__})",
                category="request_error", cause_type=type(exc).__name__) from None
        try:
            parsed = bindings.parse_completion(response)
            action = (bindings.map_holo_action(parsed.call, (frame.width, frame.height))
                      if parsed.call is not None else None)
            if parsed.reasoning:
                if action is not None:
                    action["thought"] = parsed.reasoning
            assistant = {"role": "assistant", "content": parsed.content}
            if parsed.call is not None:
                assistant["tool_calls"] = [{
                    "id": f"holo_call_{len(self._context) + 1}", "type": "function",
                    "function": deepcopy(parsed.call),
                }]
            self._context.append({"assistant": assistant, "image": image})
            history_images = getattr(api, "HISTORY_IMAGES", 2)
            self._context = [{
                "assistant": item["assistant"],
                "image": item["image"] if index >= len(self._context) - history_images else None,
            } for index, item in enumerate(self._context)]
            return action if action is not None else ExecutorNoOp()
        except Exception as exc:
            raise HoloExecutorError(
                "Holo returned an invalid native action",
                category="native_parse_error", cause_type=type(exc).__name__) from None


def make_holo_executor(bench_path, spec):
    return HoloExecutor(bench_path, HoloConfiguration(
        api_key=spec.api_key, model=spec.model, base_url=spec.base_url,
        timeout_seconds=spec.timeout_seconds, max_tokens=spec.max_tokens))
