"""One-shot OpenAI-compatible Chat Completions transport, with no SDK state."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import time
import uuid
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from handoff_recovery.core.types import PlanReply, PlanRequest
from .prompt import build_messages
from .validation import PlanValidationError, diagnose_plan_text, parse_plan, strict_json


def _planner_response_format() -> dict[str, Any]:
    """Return the four-field SlimPlan schema.

    The endpoint only enforces the small wire envelope; validation.py remains
    authoritative for branch semantics and budget rules.
    """
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "handoff_recovery_plan",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "decision": {"type": "string", "enum": ["VERIFY", "EXECUTE", "FALLBACK"]},
                    "objective": {"type": "string"},
                    "completion_conditions": {"type": "array", "items": {"type": "string"}},
                    "constraints": {"type": "array", "items": {"type": "string"}},
                    "use_prior_progress": {"type": "boolean"},
                },
                "required": ["decision", "objective", "completion_conditions", "constraints"],
                "additionalProperties": False,
            },
        },
    }


class PlannerError(RuntimeError):
    """Sanitized failure; caller owns fallback and call-budget accounting."""

    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        # Retain only a numeric HTTP status, never upstream exception text/body.
        self.http_status = http_status if type(http_status) is int and 100 <= http_status <= 599 else None


@dataclass(frozen=True)
class PlannerConfig:
    """Explicit values only. endpoint is the full URL and is never extended."""

    endpoint: str
    model: str
    api_key: str = field(repr=False)
    timeout_seconds: float = 60.0
    max_response_bytes: int = 1_048_576
    max_tokens: int = 2048
    use_environment_proxy: bool = True
    enable_thinking: bool | None = None
    response_format_mode: str = "json_schema"
    debug_response_dir: str | None = None

    def __post_init__(self) -> None:
        try:
            url = urlsplit(self.endpoint)
            valid = (
                url.scheme in {"http", "https"} and bool(url.hostname)
                and bool(url.path.strip("/")) and url.username is None
                and url.password is None and not url.fragment and not url.query
                and not any(char.isspace() or ord(char) < 32 for char in self.endpoint)
            )
            _ = url.port  # Validate malformed ports without disclosing the URL.
        except (TypeError, ValueError, AttributeError):
            valid = False
        if not valid:
            raise ValueError("endpoint must be a full HTTP(S) API URL without credentials, query, or fragment")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("model must be explicit nonempty text")
        if (not isinstance(self.api_key, str) or not self.api_key.strip()
                or any(char in self.api_key for char in "\r\n")):
            raise ValueError("api_key must be explicit nonempty single-line text")
        if (type(self.timeout_seconds) not in {float, int}
                or not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0):
            raise ValueError("timeout_seconds must be finite and positive")
        if type(self.max_response_bytes) is not int or not 1 <= self.max_response_bytes <= 16_777_216:
            raise ValueError("max_response_bytes must be between 1 and 16777216")
        if type(self.max_tokens) is not int or self.max_tokens < 1:
            raise ValueError("max_tokens must be a positive integer")
        if type(self.use_environment_proxy) is not bool:
            raise ValueError("use_environment_proxy must be boolean")
        if self.enable_thinking is not None and type(self.enable_thinking) is not bool:
            raise ValueError("enable_thinking must be boolean or None")
        if self.response_format_mode not in {"json_schema", "none"}:
            raise ValueError("response_format_mode must be json_schema or none")
        if self.debug_response_dir is not None and (not isinstance(self.debug_response_dir, str)
                                                     or not self.debug_response_dir.strip()):
            raise ValueError("debug_response_dir must be nonempty text or None")

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "PlannerConfig":
        """No environment reads, dotenv loading, aliases, or benchmark imports."""
        names = {"endpoint", "model", "api_key", "timeout_seconds", "max_response_bytes", "max_tokens",
                 "use_environment_proxy", "enable_thinking", "response_format_mode", "debug_response_dir"}
        if set(values) - names or not {"endpoint", "model", "api_key"} <= set(values):
            raise ValueError("Provide explicit endpoint, model, api_key and supported options only")
        return cls(**dict(values))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http(config: PlannerConfig, payload: dict[str, Any]) -> bytes:
    request = Request(
        config.endpoint,
        data=json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    # Keep ordinary urllib proxy behavior unless the explicit option disables it.
    proxy = ProxyHandler() if config.use_environment_proxy else ProxyHandler({})
    opener = build_opener(proxy, _NoRedirect())
    try:
        with opener.open(request, timeout=config.timeout_seconds) as response:
            if response.status != 200:
                raise PlannerError("Planner HTTP response was unsuccessful", http_status=response.status)
            body = response.read(config.max_response_bytes + 1)
    except HTTPError as error:
        status = error.code
        error.close()
        raise PlannerError(f"Planner HTTP request failed (status {status})", http_status=status) from None
    except (URLError, TimeoutError, OSError):
        raise PlannerError("Planner transport failed or timed out") from None
    if len(body) > config.max_response_bytes:
        raise PlannerError("Planner response exceeded byte limit")
    return body


def _usage(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    # Provider extras can contain arbitrary text; retain numeric cost counters only.
    return {
        key: value[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        if type(value.get(key)) is int and value[key] >= 0
    }


class OpenAICompatiblePlanner:
    """A plan() call performs at most one HTTP request, never any GUI actions."""

    def __init__(self, config: PlannerConfig, *,
                 transport: Callable[[PlannerConfig, dict[str, Any]], bytes] | None = None) -> None:
        self.config = config
        self._transport = transport or _http

    @property
    def descriptor(self) -> dict[str, Any]:
        value = {"provider": "openai_compatible", "model": self.config.model,
                 "endpoint_host": urlsplit(self.config.endpoint).hostname or "",
                 "use_environment_proxy": self.config.use_environment_proxy}
        if self.config.enable_thinking is not None:
            value["enable_thinking"] = self.config.enable_thinking
        return value

    def plan(self, request: PlanRequest) -> PlanReply:
        started = time.perf_counter()
        try:
            payload = {"model": self.config.model, "messages": build_messages(request),
                       "max_tokens": self.config.max_tokens, "stream": False, "n": 1}
            if self.config.response_format_mode == "json_schema":
                payload["response_format"] = _planner_response_format()
            if self.config.enable_thinking is not None:
                payload["enable_thinking"] = self.config.enable_thinking
            raw = self._transport(self.config, payload)
        except PlannerError as error:
            status = error.http_status
            if type(status) is int and 100 <= status <= 599:
                raise PlannerError(f"Planner HTTP request failed (status {status})", http_status=status) from None
            raise PlannerError("Planner request failed") from None
        except Exception:
            # Arbitrary transport errors may include headers, response bodies or keys.
            raise PlannerError("Planner request failed") from None
        if not isinstance(raw, bytes) or len(raw) > self.config.max_response_bytes:
            raise PlannerError("Planner response is invalid or exceeded byte limit")
        try:
            response = strict_json(raw.decode("utf-8"))
            choices = response["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError
            choice = choices[0]
            if choice.get("finish_reason") != "stop":
                raise ValueError
            message = choice["message"]
            if message.get("tool_calls") or message.get("function_call") or message.get("refusal"):
                raise ValueError
            content = message["content"]
            if not isinstance(content, str):
                raise ValueError
        except (UnicodeError, PlanValidationError, KeyError, TypeError, ValueError, AttributeError):
            raise PlannerError("Planner returned an invalid or incomplete response") from None
        try:
            plan = parse_plan(content, request)
        except PlanValidationError as error:
            self._save_debug_response(content, error, request)
            raise
        return PlanReply(plan=plan, usage=_usage(response.get("usage")),
                         elapsed_seconds=time.perf_counter() - started)

    def _save_debug_response(self, content: str, error: PlanValidationError,
                             request: PlanRequest) -> None:
        directory = self.config.debug_response_dir
        if not directory:
            return
        try:
            path = Path(directory)
            path.mkdir(parents=True, exist_ok=True)
            artifact = path / f"planner_response_{int(time.time())}_{uuid.uuid4().hex[:8]}.json"
            payload = {
                "error_type": type(error).__name__, "error_category": error.category,
                "current_frame_id": request.current_frame_id,
                "evidence_ids": [frame.evidence_id for frame in request.frames],
                "diagnostic": diagnose_plan_text(content), "raw_response": content,
            }
            artifact.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                                encoding="utf-8")
        except (OSError, TypeError, ValueError):
            # Debug capture must never change the recovery outcome.
            return
