"""Recovery Executor for HandoffBench's UI-Voyager Agent."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, fields
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from handoff_recovery.core.types import LocalInstruction, PublicFrame
from handoff_recovery.core.protocols import apply_history_policy, history_policy_descriptor
from handoff_recovery.integrations.handoffbench import load_ui_voyager_bindings
from .cloud import _current_context


class UIVoyagerExecutorError(RuntimeError):
    def __init__(self, message: str, *, category: str, cause_type: str | None = None):
        super().__init__(message)
        self.category = category
        self.cause_type = cause_type


@dataclass(frozen=True)
class UIVoyagerConfiguration:
    api_key: str = "EMPTY"
    model: str = "UI-Voyager"
    base_url: str = "http://127.0.0.1:8000/v1"
    timeout_seconds: float = 120.0
    max_tokens: int = 2048

    def __post_init__(self):
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            raise ValueError("UI-Voyager api_key must be nonempty")
        if not isinstance(self.model, str) or not self.model.strip() or any(c.isspace() for c in self.model):
            raise ValueError("UI-Voyager model must be an identifier without whitespace")
        parsed = urlsplit(self.base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or any(c.isspace() for c in self.base_url)):
            raise ValueError("UI-Voyager base_url must be a plain HTTP(S) URL")
        if type(self.timeout_seconds) not in (int, float) or not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("UI-Voyager timeout_seconds must be positive")
        if type(self.max_tokens) is not int or self.max_tokens <= 0:
            raise ValueError("UI-Voyager max_tokens must be positive")


class UIVoyagerExecutor:
    requires_completion_check = False

    def __init__(self, bench_path: str | Path, configuration: UIVoyagerConfiguration):
        if not isinstance(configuration, UIVoyagerConfiguration):
            raise TypeError("configuration must be UIVoyagerConfiguration")
        self._bench_path = bench_path
        self._configuration = configuration
        self._bindings = None
        self._task_id = None
        self._original_goal = None
        self._context: list[str] = []
        self._context_scope = None
        self._contexts: dict[str, list[str]] = {}

    def _get_bindings(self):
        if self._bindings is None:
            try:
                self._bindings = load_ui_voyager_bindings(self._bench_path)
            except Exception as exc:
                raise UIVoyagerExecutorError(
                    "HandoffBench UI-Voyager integration is unavailable",
                    category="integration_error", cause_type=type(exc).__name__) from None
        return self._bindings

    @property
    def descriptor(self):
        return {
            "adapter": "ui_voyager", "model": self._configuration.model,
            "base_url_host": urlsplit(self._configuration.base_url).hostname or "",
            "protocol": "mobile_use_tool_call", "coordinate_space": "normalized_999",
            "recovery_scopes": ["original", "verification", "execution", "fallback"],
            "history_scope": "current_task_only_retained_across_takeover",
            "requires_completion_check": False,
            "history_policy": history_policy_descriptor(),
            "budget_semantics": "shared_total_v1_gui_decisions_only",
        }

    def start_task(self, task_id: str, original_goal: str) -> None:
        if any(not isinstance(value, str) or not value.strip() for value in (task_id, original_goal)):
            raise ValueError("task_id and original_goal must be nonempty strings")
        self._task_id, self._original_goal = task_id, original_goal
        self._context, self._context_scope, self._contexts = [], None, {"original": []}

    def validate_action(self, action, width, height, *, allow_done=True):
        return self._get_bindings().validate_action(action, width, height, allow_done=allow_done)

    def _configuration_for(self, api):
        classes = [value for name, value in vars(api).items()
                   if name.endswith("Configuration") and isinstance(value, type)]
        if len(classes) != 1:
            raise UIVoyagerExecutorError("UI-Voyager configuration class is unavailable",
                                         category="integration_error")
        cls = classes[0]
        available = {field.name for field in fields(cls)}
        values = {"api_key": self._configuration.api_key, "model": self._configuration.model,
                  "base_url": self._configuration.base_url,
                  "timeout_seconds": self._configuration.timeout_seconds,
                  "max_tokens": self._configuration.max_tokens}
        return cls(**{key: value for key, value in values.items() if key in available})

    def decide(self, frame: PublicFrame, instruction: LocalInstruction) -> dict:
        if self._task_id is None:
            raise ValueError("Call start_task before deciding")
        if not isinstance(frame, PublicFrame) or not isinstance(instruction, LocalInstruction):
            raise TypeError("decide accepts only PublicFrame and LocalInstruction")
        if instruction.original_goal != self._original_goal:
            raise ValueError("The original goal cannot change within a task")
        context = _current_context(instruction)
        self._context, self._context_scope = apply_history_policy(
            self._contexts, self._context, self._context_scope, instruction)
        bindings = self._get_bindings()
        goal = self._original_goal if context is None else self._original_goal + "\n\n" + context
        try:
            with TemporaryDirectory(prefix="handoff-recovery-ui-voyager-") as directory:
                path = Path(directory) / "public.png"
                path.write_bytes(frame.png)
                raw = bindings.api.request_action(
                    str(path), goal, frame.width, frame.height,
                    configuration=self._configuration_for(bindings.api),
                    history=list(self._context),
                )
        except UIVoyagerExecutorError:
            raise
        except Exception as exc:
            raise UIVoyagerExecutorError(
                f"UI-Voyager request failed ({type(exc).__name__})",
                category="request_error", cause_type=type(exc).__name__) from None
        try:
            call = bindings.parse_tool_call(raw)
            action = bindings.map_mobile_use_action(call, (frame.width, frame.height))
            if isinstance(raw, str):
                prefix = raw.split("<tool_call>", 1)[0].strip()
                if prefix:
                    action["thought"] = prefix
            self._context.append(bindings.native_action_description(raw))
            return action
        except Exception as exc:
            raise UIVoyagerExecutorError(
                "UI-Voyager returned an invalid native action",
                category="native_parse_error", cause_type=type(exc).__name__) from None


def make_ui_voyager_executor(bench_path, spec):
    return UIVoyagerExecutor(bench_path, UIVoyagerConfiguration(
        api_key=spec.api_key, model=spec.model, base_url=spec.base_url,
        timeout_seconds=spec.timeout_seconds, max_tokens=spec.max_tokens))
