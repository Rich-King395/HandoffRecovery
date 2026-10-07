"""PhoneBuddy native executor with scoped recovery context."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable
from urllib.parse import urlsplit

from handoff_recovery.core.types import LocalInstruction, PublicFrame
from handoff_recovery.integrations.handoffbench import load_phonebuddy_bindings


class PhoneBuddyExecutorError(RuntimeError):
    """One PhoneBuddy decision failed without an internal retry."""

    def __init__(self, message: str, *, category: str, cause_type: str | None = None):
        super().__init__(message)
        self.category = category
        self.cause_type = cause_type


@dataclass(frozen=True)
class PhoneBuddyConfiguration:
    api_key: str = field(default="EMPTY", repr=False)
    model: str = "PhoneBuddy-4B"
    base_url: str = "http://127.0.0.1:8001/v1"
    timeout_seconds: float = 180.0
    max_tokens: int = 8192
    prompt_path: Path | None = None
    enable_completion_check: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            raise ValueError("PhoneBuddy api_key must be nonempty; use EMPTY for a local endpoint")
        if not isinstance(self.model, str) or not self.model.strip() or any(c.isspace() for c in self.model):
            raise ValueError("PhoneBuddy model must be an identifier without whitespace")
        try:
            url = urlsplit(self.base_url)
            valid = (isinstance(self.base_url, str) and url.scheme in {"http", "https"}
                     and url.hostname and url.username is None and url.password is None
                     and not url.query and not url.fragment
                     and not any(c.isspace() for c in self.base_url))
            url.port
        except (AttributeError, TypeError, ValueError):
            valid = False
        if not valid:
            raise ValueError("PhoneBuddy base_url must be HTTP(S) without credentials, query or fragment")
        if (type(self.timeout_seconds) not in (int, float)
                or not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0):
            raise ValueError("PhoneBuddy timeout_seconds must be finite and positive")
        if type(self.max_tokens) is not int or self.max_tokens <= 0:
            raise ValueError("PhoneBuddy max_tokens must be a positive integer")
        if self.prompt_path is not None and not isinstance(self.prompt_path, Path):
            raise TypeError("prompt_path must be a pathlib.Path or None")
        if type(self.enable_completion_check) is not bool:
            raise TypeError("enable_completion_check must be a boolean")


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


def _current_context(instruction: LocalInstruction, *, require_completion_check=True) -> str | None:
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
            "Perform only the bounded verification task below. Native finish declares "
            "verification completion, not completion of the original task."
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
            "Continue the original task using the complete remaining objective below. "
            "The prior_progress entries are orientation only; the latest screenshot is "
            "authoritative and you must re-check the visible state before acting. "
            "All original requirements and public constraints still apply. There is no "
            "further cloud planning or completion confirmation. Perform every listed "
            "inspect_before_action check before changing task data, then select the "
            "matching conditional action when one applies. Before returning done, "
            "perform this self-check in order: (1) inspect the latest screen, (2) check "
            "every completion_condition below yourself, and (3) confirm every preserve "
            "constraint still holds. A prior action, thought, or pre-handoff history is not evidence "
            "that a condition is satisfied; keep acting if any condition is not visible. "
        )
        if require_completion_check:
            guidance += (
                "If and only if every condition is satisfied, return finish with an additional "
                "completion_check array in the same tool call. Use one item per condition in order: "
                "{condition_id: c1, status: satisfied, evidence: concise visible evidence}; "
                "do not return finish without this complete array."
            )
    elif scope == "fallback":
        guidance = (
            "Resume the unchanged original task using the restored original native "
            "conversation. The Planner did not provide reliable recovery guidance. "
            "The latest screenshot is authoritative; earlier actions and thoughts are "
            "historical context only and may be stale after user takeover. Reassess "
            "the visible state before acting, preserve the original requirements, and "
            "check the original task conditions yourself before returning done."
        )
    else:
        guidance = "Continue the unchanged original task with these public constraints."
    return guidance + "\n" + json.dumps(payload, ensure_ascii=False, allow_nan=False)


class PhoneBuddyExecutor:
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
            "coordinate_space": "normalized_1000",
            "recovery_scopes": ["original", "verification", "execution", "fallback"],
            "requires_completion_check": self.requires_completion_check,
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
            instruction, require_completion_check=self.requires_completion_check)
        context_scope = "original" if instruction.resume_original else instruction.scope
        if context_scope != self._context_scope:
            if self._context_scope is not None:
                self._contexts[self._context_scope] = deepcopy(self._context)
            self._context = (deepcopy(self._contexts.get("original", []))
                             if instruction.resume_original else [])
            self._context_scope = context_scope
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
            try:
                messages = api.build_messages(path, self._original_goal, list(self._context), prompt_path=prompt_path)
            except Exception as exc:
                raise PhoneBuddyExecutorError(
                    "PhoneBuddy prompt construction failed",
                    category="prompt_error", cause_type=type(exc).__name__) from None
        if context is not None:
            messages[-1]["content"].append({"type": "text", "text": context})
        raw = self._request(messages, api)
        try:
            call, thought = bindings.parse_tool_call(raw)
        except Exception as exc:
            raise PhoneBuddyExecutorError(
                "PhoneBuddy returned an invalid native tool call",
                category="native_parse_error", cause_type=type(exc).__name__) from None
        completion_check = None
        mapping_call = call
        if call.get("name") == "finish" and "completion_check" in call.get("arguments", {}):
            mapping_call = deepcopy(call)
            mapping_args = dict(mapping_call["arguments"])
            completion_check = deepcopy(mapping_args.pop("completion_check"))
            mapping_call["arguments"] = mapping_args
        try:
            action = bindings.map_phonebuddy_action(mapping_call, (frame.width, frame.height))
        except Exception as exc:
            raise PhoneBuddyExecutorError(
                "PhoneBuddy tool call could not be mapped to a public action",
                category="action_mapping_error", cause_type=type(exc).__name__) from None
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
