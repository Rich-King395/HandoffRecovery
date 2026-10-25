"""PhoneBuddy native executor with scoped recovery context."""
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
from handoff_recovery.integrations.handoffbench import load_phonebuddy_bindings
from .config import validate_finite_positive, validate_http_url, validate_positive_int
from .condition_updates import PROTOCOL, FORMAT_SHA256, enabled, request_options, parse_native_response


class PhoneBuddyExecutorError(RuntimeError):
    """One PhoneBuddy decision failed without an internal retry."""

    def __init__(self, message: str, *, category: str, cause_type: str | None = None, recoverable: bool = False):
        super().__init__(message)
        self.category = category
        self.cause_type = cause_type
        self.recoverable = recoverable


@dataclass(frozen=True)
class PhoneBuddyConfiguration:
    api_key: str = field(default="EMPTY", repr=False)
    model: str = "PhoneBuddy-4B"
    base_url: str = "http://127.0.0.1:8001/v1"
    timeout_seconds: float = 180.0
    max_tokens: int = 8192
    prompt_path: Path | None = None
    # Completion checks are opt-in; callers can explicitly enable the A/B arm.
    enable_completion_check: bool = False
    configuration_source: str = "explicit_executor_spec"
    recovery_history_mode: str = DEFAULT_RECOVERY_HISTORY_MODE
    include_recovery_constraints: bool = False
    include_recovery_uncertain: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            raise ValueError("PhoneBuddy api_key must be nonempty; use EMPTY for a local endpoint")
        if not isinstance(self.model, str) or not self.model.strip() or any(c.isspace() for c in self.model):
            raise ValueError("PhoneBuddy model must be an identifier without whitespace")
        validate_http_url(
            self.base_url,
            "PhoneBuddy base_url must be HTTP(S) without credentials, query or fragment",
        )
        validate_finite_positive(
            self.timeout_seconds,
            "PhoneBuddy timeout_seconds must be finite and positive",
        )
        validate_positive_int(self.max_tokens, "PhoneBuddy max_tokens must be a positive integer")
        if self.prompt_path is not None and not isinstance(self.prompt_path, Path):
            raise TypeError("prompt_path must be a pathlib.Path or None")
        if type(self.enable_completion_check) is not bool:
            raise TypeError("enable_completion_check must be a boolean")
        if not isinstance(self.configuration_source, str) or not self.configuration_source.strip():
            raise ValueError("PhoneBuddy configuration_source must be nonempty text")
        if self.recovery_history_mode not in RECOVERY_HISTORY_MODES:
            raise ValueError("PhoneBuddy recovery_history_mode must be isolated or original_snapshot")
        if type(self.include_recovery_constraints) is not bool:
            raise TypeError("PhoneBuddy include_recovery_constraints must be boolean")
        if type(self.include_recovery_uncertain) is not bool:
            raise TypeError("PhoneBuddy include_recovery_uncertain must be boolean")


def _openai_class():
    try:
        from openai import OpenAI
    except ImportError:
        raise PhoneBuddyExecutorError(
            "PhoneBuddy requires the optional openai dependency",
            category="request_error", cause_type="ImportError") from None
    return OpenAI


def _response_text(message):
    content = getattr(message, "content", None)
    reasoning = getattr(message, "reasoning_content", None)
    alternate = getattr(message, "reasoning", None)
    for part in (content, reasoning, alternate):
        if part is not None and not isinstance(part, str):
            raise PhoneBuddyExecutorError(
                "PhoneBuddy completion content is not text",
                category="response_error", cause_type="TypeError")
    reasoning = reasoning or alternate
    if not content:
        return reasoning or content
    if reasoning and reasoning != content:
        return "<think>" + reasoning + "</think>\n" + content
    return content


def _current_context(instruction: LocalInstruction, *, require_completion_check=True,
                     include_constraints: bool = False,
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
    payload = {"scope": scope,
               "progress": {
                   "confirmed": list(instruction.progress.confirmed),
                   "uncertain": (list(instruction.progress.uncertain)
                                 if include_uncertain else []),
               }}
    if scope == "verification":
        task = instruction.verification
        payload["verification"] = {
            "objective": task.objective,
            "required_evidence": list(task.required_evidence),
            "constraints": (list(task.constraints) if include_constraints else []),
            "final_confirmation": task.final_confirmation,
        }
    elif scope == "execution":
        handoff = instruction.handoff
        payload["handoff"] = {
            "remaining_objective": handoff.remaining_objective,
            "preserve": (list(handoff.preserve) if include_constraints else []),
            "completion_conditions": list(handoff.completion_conditions),
        }
        if require_completion_check:
            payload["completion_check_required"] = True
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


class PhoneBuddyExecutor:
    condition_updates_protocol = PROTOCOL
    # The normalized HandoffRecovery action contract is stricter than the
    # unchanged upstream PhoneBuddy action protocol only during recovery.
    requires_completion_check = True

    def __init__(self, bench_path: str | Path, configuration: PhoneBuddyConfiguration,
                 *, client_factory: Callable | None = None):
        if not isinstance(bench_path, (str, Path)) or not str(bench_path).strip():
            raise ValueError("An explicit Benchmark checkout path is required")
        if not isinstance(configuration, PhoneBuddyConfiguration):
            raise TypeError("configuration must be PhoneBuddyConfiguration")
        self._bench_path = bench_path
        self._configuration = configuration
        self._client_factory = client_factory
        self.requires_completion_check = configuration.enable_completion_check
        self._bindings = None
        self._task_id: str | None = None
        self._original_goal: str | None = None
        self._context: list[dict[str, str]] = []
        self._context_scope: str | None = None
        self._contexts: dict[str, list[dict[str, str]]] = {}

    @property
    def descriptor(self):
        """Stable, non-secret metadata for experiment manifests."""
        return {
            "adapter": "phonebuddy",
            "model": self._configuration.model,
            "base_url_host": urlsplit(self._configuration.base_url).hostname or "",
            "protocol": "phonebuddy_finish",
            "native_protocol": "phonebuddy_tool_call_v1",
            "action_normalization": "native_parser_then_handoffbench_mapper",
            "request_entrypoint": "handoffbench.api.request_action",
            "response_extraction": "handoffbench.api._response_text",
            "native_history_serialization": "handoffbench.agent.history_entry",
            "configuration_source": self._configuration.configuration_source,
            "coordinate_space": "normalized_1000",
            "recovery_scopes": ["original", "verification", "execution", "fallback"],
            "requires_completion_check": self.requires_completion_check,
            "recovery_constraints_in_prompt": self._configuration.include_recovery_constraints,
            "recovery_uncertain_in_prompt": self._configuration.include_recovery_uncertain,
            "history_policy": history_policy_descriptor(self._configuration.recovery_history_mode),
            "history_owner": "handoff_recovery_host_policy",
            "condition_updates_protocol": PROTOCOL,
            "condition_updates_format_sha256": FORMAT_SHA256,
            "budget_semantics": "shared_total_v1_gui_decisions_only",
        }

    def start_task(self, task_id: str, original_goal: str) -> None:
        if any(not isinstance(value, str) or not value.strip() for value in (task_id, original_goal)):
            raise ValueError("task_id and original_goal must be nonempty strings")
        self._task_id = task_id
        self._original_goal = original_goal
        self._context = []
        self._context_scope = None
        self._contexts = {"original": []}

    def validate_action(self, action, width, height, *, allow_done=True):
        """Validate the normalized action using PhoneBuddy's public adapter."""
        if self._bindings is None:
            self._bindings = load_phonebuddy_bindings(self._bench_path)
        return self._bindings.validate_action(action, width, height, allow_done=allow_done)

    def decide(self, frame: PublicFrame, instruction: LocalInstruction) -> dict:
        if self._task_id is None:
            raise ValueError("Call start_task before deciding")
        if not isinstance(frame, PublicFrame) or not isinstance(instruction, LocalInstruction):
            raise TypeError("decide accepts only PublicFrame and LocalInstruction")
        if instruction.original_goal != self._original_goal:
            raise ValueError("The original goal cannot change within a task")
        context = _current_context(
            instruction,
            require_completion_check=self.requires_completion_check,
            include_constraints=self._configuration.include_recovery_constraints,
            include_uncertain=self._configuration.include_recovery_uncertain,
        )
        self._context, self._context_scope = apply_history_policy(
            self._contexts, self._context, self._context_scope, instruction,
            recovery_history_mode=self._configuration.recovery_history_mode)
        if self._bindings is None:
            try:
                self._bindings = load_phonebuddy_bindings(self._bench_path)
            except Exception as exc:
                raise PhoneBuddyExecutorError(
                    "PhoneBuddy Benchmark integration is unavailable",
                    category="integration_error", cause_type=type(exc).__name__) from None
        bindings = self._bindings
        api = bindings.api
        prompt_path = self._configuration.prompt_path
        if prompt_path is None:
            prompt_path = Path(self._bench_path) / ".runtime/phonebuddy/phonebuddy_system_prompt.txt"
        with TemporaryDirectory(prefix="handoff-recovery-phonebuddy-") as directory:
            path = Path(directory) / "public.png"
            path.write_bytes(frame.png)
            request_goal = recovery_task_goal(self._original_goal, context)
            if (context is not None and context.get("scope") == "execution"
                    and self.requires_completion_check):
                request_goal += (
                    " Return finish only with one completion_check item for each listed "
                    "condition, in the same order, using concise visible evidence."
                )
            if self._client_factory is None and callable(getattr(bindings, "request_action", None)):
                # The production path is owned by HandoffBench. Recovery only
                # supplies the scoped goal/context and native history.
                try:
                    raw = bindings.request_action(
                        path, self._original_goal, configuration=self._configuration,
                        history=list(self._context), recovery_context=context)
                except Exception as exc:
                    raise PhoneBuddyExecutorError(
                        "PhoneBuddy request failed",
                        category="request_error", cause_type=type(exc).__name__) from None
            else:
                # Kept only for injected offline test clients.
                try:
                    messages = api.build_messages(path, request_goal,
                                                  list(self._context), prompt_path=prompt_path,
                                                  **request_options(instruction))
                except Exception as exc:
                    raise PhoneBuddyExecutorError(
                        "PhoneBuddy prompt construction failed",
                        category="prompt_error", cause_type=type(exc).__name__) from None
                raw = self._request(messages, api)
        try:
            (call, thought), raw, updates = parse_native_response(
                raw, bindings.parse_tool_call, active=enabled(instruction))
        except Exception as exc:
            raise PhoneBuddyExecutorError(
                "PhoneBuddy returned an invalid native tool call",
                category="native_parse_error", cause_type=type(exc).__name__,
                recoverable=isinstance(exc, (ValueError, OverflowError))) from None
        completion_check = None
        mapping_call = call
        if (instruction.completion_policy != "native" and call.get("name") == "finish"
                and "completion_check" in call.get("arguments", {})):
            mapping_call = deepcopy(call)
            mapping_args = dict(mapping_call["arguments"])
            completion_check = deepcopy(mapping_args.pop("completion_check"))
            mapping_call["arguments"] = mapping_args
        try:
            action = bindings.map_phonebuddy_action(mapping_call, (frame.width, frame.height))
        except Exception as exc:
            reason = str(exc).strip()
            suffix = f": {reason[:240]}" if reason else ""
            raise PhoneBuddyExecutorError(
                f"PhoneBuddy tool call could not be mapped to a public action{suffix}",
                category="action_mapping_error", cause_type=type(exc).__name__,
                recoverable=isinstance(exc, (ValueError, OverflowError))) from None
        try:
            bindings.validate_action(action, frame.width, frame.height, allow_done=True)
        except Exception as exc:
            reason = str(exc).strip()
            suffix = f": {reason[:240]}" if reason else ""
            raise PhoneBuddyExecutorError(
                f"PhoneBuddy mapped action failed HandoffBench validation{suffix}",
                category="action_mapping_error", cause_type=type(exc).__name__,
                recoverable=isinstance(exc, (ValueError, OverflowError))) from None
        if thought:
            action["thought"] = thought
        if completion_check is not None:
            action["completion_check"] = completion_check
        try:
            self._context.append(bindings.history_entry(call, thought))
        except Exception as exc:
            raise PhoneBuddyExecutorError(
                "PhoneBuddy native history could not be serialized",
                category="history_error", cause_type=type(exc).__name__) from None
        if updates is not None:
            action["condition_updates"] = updates
        return action

    def _request(self, messages, api) -> str:
        configuration = self._configuration
        factory = self._client_factory or _openai_class()
        try:
            with factory(api_key=configuration.api_key, base_url=configuration.base_url,
                         timeout=configuration.timeout_seconds, max_retries=0) as client:
                response = client.chat.completions.create(
                    model=configuration.model, messages=messages, stream=False,
                    max_completion_tokens=configuration.max_tokens,
                    temperature=getattr(api, "TEMPERATURE", 0.0),
                    top_p=getattr(api, "TOP_P", 1.0),
                    extra_body={
                        "top_k": getattr(api, "TOP_K", -1),
                        "repetition_penalty": 1.0,
                        "skip_special_tokens": False,
                        "chat_template_kwargs": {"enable_thinking": True},
                    },
                )
        except Exception as error:
            status = getattr(error, "status_code", None)
            suffix = f", HTTP {status}" if type(status) is int and 100 <= status <= 599 else ""
            raise PhoneBuddyExecutorError(
                f"PhoneBuddy request failed ({type(error).__name__}{suffix})",
                category="request_error", cause_type=type(error).__name__) from None
        try:
            return _response_text(response.choices[0].message)
        except PhoneBuddyExecutorError:
            raise
        except (AttributeError, IndexError, TypeError) as exc:
            raise PhoneBuddyExecutorError(
                "PhoneBuddy response is missing its completion message",
                category="response_error", cause_type=type(exc).__name__) from None
