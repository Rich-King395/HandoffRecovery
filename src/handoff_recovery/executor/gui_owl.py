"""One-request GUI-Owl decisions with explicit, scoped recovery context.

Only ``start_task`` resets native history. Verification and recovery share
the original goal and all valid own outputs, including four prior images.
The coordinator counts every attempted decision and interprets done/fail in
its current scope; this adapter never executes a device action or retries.

The HandoffRecoveryAgent bridge connects this executor to Benchmark
``run_task`` through public control notifications and scoped completion.
"""
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
from handoff_recovery.integrations.handoffbench import load_gui_owl_bindings


class ExecutorError(RuntimeError):
    """One local decision failed; callers must still count that attempt."""


@dataclass(frozen=True)
class GUIOwlConfiguration:
    """Explicit client settings; no environment or Benchmark .env is read."""

    api_key: str = field(default="EMPTY", repr=False)
    model: str = "GUI-Owl-1.5-4B-Instruct"
    base_url: str = "http://127.0.0.1:8002/v1"
    timeout_seconds: float = 180.0
    max_tokens: int = 2048

    def __post_init__(self) -> None:
        if not isinstance(self.api_key, str) or not self.api_key.strip():
            raise ValueError("GUI-Owl api_key must be nonempty; use EMPTY for a local endpoint")
        if (not isinstance(self.model, str) or not self.model
                or any(char.isspace() for char in self.model)):
            raise ValueError("GUI-Owl model must be an identifier without whitespace")
        try:
            url = urlsplit(self.base_url)
            valid = (isinstance(self.base_url, str) and url.scheme in {"http", "https"}
                     and url.hostname and url.username is None and url.password is None
                     and not url.query and not url.fragment
                     and not any(char.isspace() for char in self.base_url))
            url.port
        except (AttributeError, TypeError, ValueError):
            valid = False
        if not valid:
            raise ValueError("GUI-Owl base_url must be HTTP(S) without credentials, query or fragment")
        if (type(self.timeout_seconds) not in (int, float)
                or not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0):
            raise ValueError("GUI-Owl timeout_seconds must be finite and positive")
        if type(self.max_tokens) is not int or self.max_tokens <= 0:
            raise ValueError("GUI-Owl max_tokens must be a positive integer")


def _openai_class():
    try:
        from openai import OpenAI
    except ImportError:
        raise ExecutorError("GUI-Owl requires the optional openai dependency") from None
    return OpenAI


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
            "For this decision perform only the bounded verification task below. "
            "Inspect visible evidence and obey its action constraints. "
            "Native answer or terminate(success) declares verification complete only; "
            "it does not declare the original task complete. Native terminate(failure) "
            "reports inability to complete this verification."
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
            "All original requirements and public constraints still govern completion. "
            "Perform every listed inspect_before_action check before changing task data, "
            "then resolve conditional_actions locally as part of continuous execution. "
            "Before returning native completion, inspect the latest screen and check "
            "every completion_condition below yourself. A prior action, thought, or "
            "pre-handoff history is not evidence that a condition is satisfied; keep "
            "acting if any condition is not visible. Use native completion/failure for "
            "the original task; there is no further cloud planning or confirmation."
        )
    elif scope == "fallback":
        guidance = (
            "Resume the unchanged original task using the restored original native "
            "conversation. The Planner did not provide reliable recovery guidance. "
            "The latest screenshot is authoritative; earlier actions and thoughts are "
            "historical context only and may be stale after user takeover. Reassess "
            "the visible state before acting, preserve the original requirements, and "
            "check the original task conditions yourself before native completion."
        )
    else:
        guidance = "Continue the unchanged original task with these public constraints."
    return (
        "Current decision context (applies only to this latest observation):\n"
        + guidance + " Keep the native Action/mobile_use text protocol.\n"
        + json.dumps(payload, ensure_ascii=False, allow_nan=False)
    )


class GUIOwlExecutor:
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
            "coordinate_space": "image_pixels",
            "recovery_scopes": ["original", "verification", "execution", "fallback"],
            "requires_completion_check": False,
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
            self._bindings = load_gui_owl_bindings(self._bench_path)
        return self._bindings.validate_action(action, width, height, allow_done=allow_done)

    def decide(self, frame: PublicFrame, instruction: LocalInstruction) -> dict:
        """Return exactly one mapped action, or raise without a retry or action."""
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
        if self._bindings is None:
            self._bindings = load_gui_owl_bindings(self._bench_path)
        bindings = self._bindings
        api = bindings.api
        size = (frame.width, frame.height)
        bindings.validate_action({"action": "done"}, *size, allow_done=True)
        # Benchmark accepts paths; this private temporary PNG is written only
        # from immutable public bytes and deleted before the model is contacted.
        with TemporaryDirectory(prefix="handoff-recovery-frame-") as directory:
            path = Path(directory) / "public.png"
            path.write_bytes(frame.png)
            image = api.encode_screenshot(path, expected_size=size)
        messages = api.build_messages(image, self._original_goal, list(self._context))
        if context is not None:
            messages[-1]["content"].append({"type": "text", "text": context})
        raw = self._request(messages)
        try:
            call, target = bindings.parse_tool_call(raw)
            action = bindings.map_mobile_use_action(call, size)
            action["target"] = target
            bindings.validate_action(action, *size, allow_done=True)
        except (ValueError, OverflowError, RecursionError, TypeError):
            raise ExecutorError("GUI-Owl returned an invalid native decision") from None
        history = self._context + [{"output": raw, "image": image}]
        self._context = [
            {"output": item["output"],
             "image": item["image"] if index >= len(history) - api.HISTORY_IMAGES else None}
            for index, item in enumerate(history)
        ]
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
        except (AttributeError, IndexError, TypeError):
            raise ExecutorError("GUI-Owl response is missing its completion message") from None
        if not isinstance(content, str) or not content.strip() or tool_calls:
            raise ExecutorError("GUI-Owl requires a nonempty native text completion")
        return content
