"""Recovery Executor for HandoffBench's MAIGym Agent."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, fields
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from handoff_recovery.core.types import LocalInstruction, PublicFrame
from handoff_recovery.core.protocols import (apply_history_policy, history_policy_descriptor,
                                              DEFAULT_RECOVERY_HISTORY_MODE,
                                              RECOVERY_HISTORY_MODES, recovery_task_goal)
from handoff_recovery.integrations.handoffbench import load_mai_gym_bindings
from .cloud import _current_context
from .condition_updates import enabled
from .config import validate_finite_positive, validate_http_url, validate_positive_int


class MAIGymExecutorError(RuntimeError):
    def __init__(self, message: str, *, category: str, cause_type: str | None = None, recoverable: bool = False):
        super().__init__(message)
        self.category = category
        self.cause_type = cause_type
        self.recoverable = recoverable


@dataclass(frozen=True)
class MAIGymConfiguration:
    max_tokens: int
    api_key: str = field(default="EMPTY", repr=False)
    model: str = "MAI-Gym-9B"
    base_url: str = "http://127.0.0.1:8014/v1"
    timeout_seconds: float = 600.0
    temperature: float = 0.0
    top_p: float = 1.0
    recovery_history_mode: str = DEFAULT_RECOVERY_HISTORY_MODE
    configuration_source: str = "explicit_executor_spec"

    def __post_init__(self):
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            raise ValueError("MAIGym api_key must be nonempty")
        if not isinstance(self.model, str) or not self.model.strip() or any(c.isspace() for c in self.model):
            raise ValueError("MAIGym model must be an identifier without whitespace")
        validate_http_url(self.base_url, "MAIGym base_url must be a plain HTTP(S) URL")
        validate_finite_positive(self.timeout_seconds, "MAIGym timeout_seconds must be positive")
        validate_positive_int(self.max_tokens, "MAIGym max_tokens must be positive")
        if type(self.temperature) not in (int, float) or not 0 <= self.temperature <= 2:
            raise ValueError("MAIGym temperature must be within [0,2]")
        if type(self.top_p) not in (int, float) or not 0 < self.top_p <= 1:
            raise ValueError("MAIGym top_p must be within (0,1]")
        if self.recovery_history_mode not in RECOVERY_HISTORY_MODES:
            raise ValueError("MAIGym recovery_history_mode must be isolated or original_snapshot")
        if not isinstance(self.configuration_source, str) or not self.configuration_source.strip():
            raise ValueError("MAIGym configuration_source must be nonempty text")


class MAIGymExecutor:
    condition_updates_protocol = "unsupported"
    requires_completion_check = False

    def __init__(self, bench_path: str | Path, configuration: MAIGymConfiguration):
        if not isinstance(configuration, MAIGymConfiguration):
            raise TypeError("configuration must be MAIGymConfiguration")
        self._bench_path = bench_path
        self._configuration = configuration
        self._bindings = None
        self._task_id = None
        self._original_goal = None
        self._context: list[dict] = []
        self._context_scope = None
        self._contexts: dict[str, list[dict]] = {}
        self._scope_goals: dict[str, str] = {}

    def _get_bindings(self):
        if self._bindings is None:
            try:
                self._bindings = load_mai_gym_bindings(self._bench_path)
            except Exception as exc:
                raise MAIGymExecutorError(
                    "HandoffBench MAIGym integration is unavailable",
                    category="integration_error", cause_type=type(exc).__name__) from None
        return self._bindings

    @property
    def descriptor(self):
        return {
            "adapter": "mai_gym", "model": self._configuration.model,
            "base_url_host": urlsplit(self._configuration.base_url).hostname or "",
            "protocol": "mai_gym_mobile_use_json_action_type", "coordinate_space": "normalized_999",
            "coordinate_mapping": "min(int(float(c)/999*axis_size),axis_size-1)",
            "max_retries": 0, "image_transport": "original_png", "image_resize": "none",
            "temperature": self._configuration.temperature, "top_p": self._configuration.top_p,
            "enable_thinking": "checkpoint_template_default_true", "preserve_thinking": False,
            "history_format": "native_user_screenshots_assistant_responses",
            "history_profile": "windows_wise", "window_size": 10, "overlap": 2,
            "task_query": "first_user_message_only", "continuous_drag_supported": True,
            "formal_readiness": "pending_real_service_input_drag_smoke",
            "output_limit_policy": "required_explicit_setting",
            "recovery_scopes": ["original", "verification", "execution", "fallback"],
            "history_scope": "current_scope_only_by_default",
            "requires_completion_check": False,
            "configuration_source": self._configuration.configuration_source,
            "timeout_seconds": self._configuration.timeout_seconds,
            "max_tokens": self._configuration.max_tokens,
            "history_policy": history_policy_descriptor(self._configuration.recovery_history_mode),
            "history_owner": "handoff_recovery_host_policy",
            "condition_updates_supported": False,
            "budget_semantics": "shared_total_v1_gui_decisions_only",
        }

    def start_task(self, task_id: str, original_goal: str) -> None:
        if any(not isinstance(value, str) or not value.strip() for value in (task_id, original_goal)):
            raise ValueError("task_id and original_goal must be nonempty strings")
        self._task_id, self._original_goal = task_id, original_goal
        self._context, self._context_scope, self._contexts = [], None, {"original": []}
        self._scope_goals = {}

    def validate_action(self, action, width, height, *, allow_done=True):
        return self._get_bindings().validate_action(action, width, height, allow_done=allow_done)

    def _configuration_for(self, api):
        classes = [value for name, value in vars(api).items()
                   if name.endswith("Configuration") and isinstance(value, type)]
        if len(classes) != 1:
            raise MAIGymExecutorError("MAIGym configuration class is unavailable",
                                         category="integration_error")
        cls = classes[0]
        available = {field.name for field in fields(cls)}
        values = {"api_key": self._configuration.api_key, "model": self._configuration.model,
                  "base_url": self._configuration.base_url,
                  "timeout_seconds": self._configuration.timeout_seconds,
                  "max_tokens": self._configuration.max_tokens,
                  "temperature": self._configuration.temperature, "top_p": self._configuration.top_p}
        return cls(**{key: value for key, value in values.items() if key in available})

    def decide(self, frame: PublicFrame, instruction: LocalInstruction) -> dict:
        if self._task_id is None:
            raise ValueError("Call start_task before deciding")
        if not isinstance(frame, PublicFrame) or not isinstance(instruction, LocalInstruction):
            raise TypeError("decide accepts only PublicFrame and LocalInstruction")
        if instruction.original_goal != self._original_goal:
            raise ValueError("The original goal cannot change within a task")
        if (enabled(instruction) or (instruction.scope == "execution"
                and (instruction.completion_policy != "native" or instruction.condition_updates_enabled is not False))):
            raise ValueError("MAIGym requires native completion and condition_updates explicitly disabled")
        context = _current_context(instruction)
        self._context, self._context_scope = apply_history_policy(
            self._contexts, self._context, self._context_scope, instruction,
            recovery_history_mode=self._configuration.recovery_history_mode)
        bindings = self._get_bindings()
        goal = self._scope_goals.setdefault(
            self._context_scope, recovery_task_goal(self._original_goal, context))
        try:
            image = bindings.api.encode_screenshot(frame.png, frame.width, frame.height)
            with TemporaryDirectory(prefix="handoff-recovery-mai_gym-") as directory:
                path = Path(directory) / "public.png"
                path.write_bytes(frame.png)
                raw = bindings.api.request_action(
                    str(path), goal, frame.width, frame.height,
                    configuration=self._configuration_for(bindings.api),
                    history=deepcopy(self._context),
                )
        except MAIGymExecutorError:
            raise
        except Exception as exc:
            raise MAIGymExecutorError(
                f"MAIGym request failed ({type(exc).__name__})",
                category="request_error", cause_type=type(exc).__name__) from None
        turn = {"image": image, "raw_response": None}
        self._context.append(turn)
        try:
            call, thought, canonical = bindings.parse_completion(raw)
        except Exception as exc:
            raise MAIGymExecutorError(
                "MAIGym returned an invalid native function call",
                category="native_parse_error", cause_type=type(exc).__name__,
                recoverable=isinstance(exc, (ValueError, TypeError, OverflowError, RecursionError))) from None
        try:
            action = bindings.map_mai_gym_action(call, (frame.width, frame.height))
            bindings.validate_action(action, frame.width, frame.height, allow_done=True)
            if thought:
                action["thought"] = thought
        except Exception as exc:
            raise MAIGymExecutorError(
                "MAIGym action could not be mapped or validated",
                category="action_mapping_error", cause_type=type(exc).__name__,
                recoverable=isinstance(exc, (ValueError, TypeError, OverflowError, RecursionError))) from None
        try:
            turn["raw_response"] = canonical
        except Exception as exc:
            raise MAIGymExecutorError(
                "MAIGym native history could not be serialized",
                category="history_error", cause_type=type(exc).__name__) from None
        return action


def make_mai_gym_executor(bench_path, spec):
    from .registry import create_executor
    return create_executor("mai_gym", bench_path, spec)
