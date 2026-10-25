"""Small, explicit messages shared by the prototype components.

Callers are responsible for supplying only normal agent-visible observations.
These objects enforce data shape, not an OS security boundary or visual truth.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import struct
from collections.abc import Mapping


DEFAULT_TOTAL_DECISIONS = 30
# Must remain compatible with HandoffBench's shared_total_v1 task budget.
GUI_BUDGET_POLICY_VERSION = "shared_total_v1"
GUI_BUDGET_SEMANTICS = {
    "version": GUI_BUDGET_POLICY_VERSION,
    "total": "one_shared_gui_decision_budget_across_original_prefix_resume_and_recovery",
    "planner_calls": "separate_preparation_counter_not_gui_decisions",
    "verification": "verification_actions_consume_the_same_gui_budget",
    "takeover_actions": "do_not_consume_gui_decisions",
    "refill_on_takeover": False,
}
# Backwards-compatible name for callers that only need the current default.
# Runtime validation must use RecoveryBudget.total_decisions instead.
TOTAL_DECISIONS = DEFAULT_TOTAL_DECISIONS
MAX_FRAME_BYTES = 16 * 1024 * 1024
COMPLETION_CHECK_STATUSES = frozenset({"satisfied", "unsatisfied", "unverified"})
RECOVERY_CONDITION_STATUSES = frozenset({"pending", "satisfied", "unverifiable"})

PROGRESS_MAX_ITEMS = 8
PROGRESS_MAX_CHARS = 160
_PROGRESS_FIRST_PERSON = re.compile(
    r"(?:\b(?:i|me|my|mine|we|us|our|ours)\b|\b(?:i'm|i've|i'd|we're|we've)\b|"
    r"我|我的|我们|本模型|本代理)", re.IGNORECASE)
_PROGRESS_REASONING = re.compile(
    r"(?:\b(?:because|therefore|thus|hence|since|as a result|based on|assuming|"
    r"reasoning|analysis|infer(?:red|ence)?|suggest(?:s|ed)?|indicat(?:e|es|ed)|"
    r"conclude(?:d)?|it follows)\b|由于|因此|所以|推理|分析|推断|推测|说明|意味着)",
    re.IGNORECASE,
)
_PROGRESS_GUESS = re.compile(
    r"(?:\b(?:probably|likely|maybe|might|possibly|perhaps|apparently|seems?|"
    r"appears?|could be)\b|可能|大概|也许|似乎|看起来|应该是|猜测)", re.IGNORECASE,
)
_PROGRESS_ACTION = re.compile(
    r"(?:^(?:please|tap|click|press|open|select|enter|type|save|delete|remove|"
    r"swipe|launch|close|inspect|verify|check|continue|resume|go to|choose)\b|"
    r"^(?:请|点击|打开|选择|输入|保存|删除|移除|滑动|启动|关闭|检查|验证|继续|返回|前往))",
    re.IGNORECASE,
)
_PROGRESS_COMPOSITE = re.compile(
    r"(?:;|\s(?:and|but|while|although|then)\s|\s(?:以及|并且|同时|然后)\s)",
    re.IGNORECASE,
)
_PROGRESS_UNCERTAIN = re.compile(
    r"(?:\b(?:unknown|uncertain|unconfirmed|not confirmed|cannot confirm|"
    r"requires confirmation|needs verification|not established)\b|"
    r"无法确认|不确定|待确认|需要核实|尚未确认)", re.IGNORECASE,
)

# Public device actions accepted by HandoffBench's
# ``resumebench.devices.android.validate_action``.  The Coordinator uses this
# name-level set only; dimensions, fields and value ranges remain the host
# validator's responsibility because the core has no device geometry.
HANDOFFBENCH_ACTIONS = frozenset({
    "tap", "text", "double_tap", "swipe", "drag", "long_press",
    "replace_text", "type_and_enter", "append_text", "save_screenshot",
    "keyevent", "back", "home", "wait", "launch_app", "close_app",
    "done", "fail",
})


@dataclass(frozen=True)
class RecoveryBudget:
    """Runtime limits supplied by the host for one recovery task.

    The default preserves the current HandoffBench experiment policy. The
    coordinator never assumes that future hosts use the same total budget.
    """

    total_decisions: int = DEFAULT_TOTAL_DECISIONS
    max_cloud_calls: int = 2
    max_verification_rounds: int = 1

    def __post_init__(self):
        if type(self.total_decisions) is not int or self.total_decisions < 1:
            raise ValueError("total_decisions must be a positive integer")
        if type(self.max_cloud_calls) is not int or self.max_cloud_calls < 0:
            raise ValueError("max_cloud_calls must be a nonnegative integer")
        if type(self.max_verification_rounds) is not int or self.max_verification_rounds < 0:
            raise ValueError("max_verification_rounds must be a nonnegative integer")


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be nonempty text")


def _texts(values, name):
    if not isinstance(values, tuple):
        raise ValueError(f"{name} must be a tuple")
    for value in values:
        _text(value, name)


def validate_completion_check(value, conditions):
    """Validate a local model's per-condition completion declaration.

    This checks the public protocol shape and coverage only. It cannot prove
    that the supplied screenshot evidence is visually truthful.
    """
    if not isinstance(conditions, tuple) or not conditions or any(
            not isinstance(item, str) or not item.strip() for item in conditions):
        raise ValueError("Completion conditions must be a nonempty text tuple")
    if not isinstance(value, list):
        raise ValueError("completion_check must be an array")
    expected = tuple(f"c{index}" for index in range(1, len(conditions) + 1))
    if len(value) != len(expected):
        raise ValueError("completion_check must cover every completion condition exactly once")
    seen = []
    normalized = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"condition_id", "status", "evidence"}:
            raise ValueError("completion_check item has an invalid shape")
        condition_id = item["condition_id"]
        status = item["status"]
        evidence = item["evidence"]
        if not isinstance(condition_id, str) or condition_id not in expected:
            raise ValueError("completion_check contains an unknown condition_id")
        if condition_id in seen:
            raise ValueError("completion_check contains duplicate condition_id")
        if not isinstance(status, str) or status not in COMPLETION_CHECK_STATUSES:
            raise ValueError("completion_check contains an invalid status")
        if not isinstance(evidence, str) or not evidence.strip():
            raise ValueError("completion_check evidence must be nonempty text")
        seen.append(condition_id)
        normalized.append({"condition_id": condition_id, "status": status,
                           "evidence": evidence})
    if tuple(seen) != expected:
        raise ValueError("completion_check must preserve condition order")
    if any(item["status"] != "satisfied" for item in normalized):
        raise ValueError("done requires every completion condition to be satisfied")
    return tuple(normalized)


def validate_condition_updates(value, conditions):
    """Validate incremental model claims for newly satisfied conditions.

    The update payload is a delta: omitted conditions remain ``pending``.
    ``unverifiable`` is intentionally not a model update because it does not
    advance the execution worklist; the full condition state may still use
    that status for other controller paths.
    """
    if value is None:
        return ()
    if not isinstance(conditions, tuple) or not conditions:
        raise ValueError("Condition updates require nonempty completion conditions")
    if not isinstance(value, list):
        raise ValueError("condition_updates must be an array")
    expected = {f"c{index}": condition for index, condition in enumerate(conditions, 1)}
    seen = set()
    normalized = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"condition_id", "status", "evidence"}:
            raise ValueError("condition_updates item has an invalid shape")
        condition_id, status, evidence = (item["condition_id"], item["status"], item["evidence"])
        if condition_id not in expected or condition_id in seen:
            raise ValueError("condition_updates contains an unknown or duplicate condition_id")
        if status != "satisfied":
            raise ValueError("condition_updates contains an invalid status")
        if not isinstance(evidence, str) or not evidence.strip():
            raise ValueError("condition_updates evidence must be nonempty text")
        seen.add(condition_id)
        normalized.append({"condition_id": condition_id, "status": status,
                           "condition": expected[condition_id], "evidence": evidence})
    return tuple(normalized)


@dataclass(frozen=True)
class PublicFrame:
    evidence_id: str
    png: bytes = field(repr=False)
    width: int
    height: int
    note: str = ""

    def __post_init__(self):
        if not isinstance(self.evidence_id, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,79}", self.evidence_id):
            raise ValueError("Invalid evidence ID")
        if self.evidence_id.startswith("action_"):
            raise ValueError("Frame IDs must not use reserved action_ prefix")
        if (not isinstance(self.png, bytes) or not 33 <= len(self.png) <= MAX_FRAME_BYTES
                or self.png[:8] != b"\x89PNG\r\n\x1a\n" or self.png[12:16] != b"IHDR"):
            raise ValueError("A bounded PNG snapshot is required")
        if any(type(v) is not int or v <= 0 for v in (self.width, self.height)):
            raise ValueError("Frame dimensions must be positive integers")
        if struct.unpack(">II", self.png[16:24]) != (self.width, self.height):
            raise ValueError("PNG header dimensions do not match the public frame")
        if not isinstance(self.note, str):
            raise ValueError("Frame note must be text")

    @classmethod
    def from_png(cls, evidence_id, path, *, note=""):
        with Path(path).open("rb") as stream:
            data = stream.read(MAX_FRAME_BYTES + 1)
        if len(data) < 24:
            raise ValueError("Invalid PNG snapshot")
        width, height = struct.unpack(">II", data[16:24])
        return cls(evidence_id, data, width, height, note)

    def metadata(self):
        return {"evidence_id": self.evidence_id, "width": self.width, "height": self.height,
                "note": self.note, "sha256": hashlib.sha256(self.png).hexdigest()}


@dataclass(frozen=True)
class OwnAction:
    decision_number: int
    action: dict
    scope: str
    frame_id: str
    result: str = "unknown"
    state_changed: bool | None = None

    def __post_init__(self):
        if type(self.decision_number) is not int or self.decision_number < 1:
            raise ValueError("Invalid own decision number")
        if not isinstance(self.action, dict):
            raise ValueError("Own action must be a JSON object")
        json.dumps(self.action, allow_nan=False)
        object.__setattr__(self, "action", deepcopy(self.action))
        _text(self.scope, "action scope")
        _text(self.frame_id, "action frame ID")
        _text(self.result, "action result")
        if self.state_changed is not None and type(self.state_changed) is not bool:
            raise TypeError("state_changed must be a boolean or None")


@dataclass(frozen=True)
class VerificationTask:
    objective: str
    required_evidence: tuple[str, ...]
    constraints: tuple[str, ...]
    final_confirmation: bool = False

    def __post_init__(self):
        _text(self.objective, "verification objective")
        _texts(self.required_evidence, "required evidence")
        _texts(self.constraints, "verification constraints")
        if not self.required_evidence:
            raise ValueError("Verification must specify visible evidence")
        if type(self.final_confirmation) is not bool:
            raise TypeError("final_confirmation must be boolean")


@dataclass(frozen=True)
class ProgressSummary:
    """Evidence-backed orientation about work before recovery.

    ``confirmed`` contains only facts supported by supplied observations or
    acknowledged successful actions. ``uncertain`` records state that still
    needs checking; neither collection is a completion declaration.
    """

    confirmed: tuple[str, ...] = ()
    uncertain: tuple[str, ...] = ()

    def __post_init__(self):
        _texts(self.confirmed, "confirmed progress")
        _texts(self.uncertain, "uncertain progress")


def sanitize_progress(value: ProgressSummary | Mapping[str, object]) -> ProgressSummary:
    """Bound progress and drop reasoning or action-like Planner text."""
    if isinstance(value, ProgressSummary):
        source = {"confirmed": value.confirmed, "uncertain": value.uncertain}
    elif isinstance(value, Mapping):
        if set(value) != {"confirmed", "uncertain"}:
            raise ValueError("progress must contain only confirmed and uncertain")
        source = value
    else:
        raise TypeError("progress must be a ProgressSummary or mapping")

    confirmed: list[str] = []
    uncertain: list[str] = []
    moved: list[str] = []

    def normalize(item, bucket):
        if not isinstance(item, str):
            return None, None
        text = " ".join(item.split()).strip()[:PROGRESS_MAX_CHARS].rstrip()
        if (not text or _PROGRESS_FIRST_PERSON.search(text)
                or _PROGRESS_REASONING.search(text)
                or _PROGRESS_GUESS.search(text)
                or _PROGRESS_ACTION.search(text)
                or _PROGRESS_COMPOSITE.search(text)):
            return None, None
        if bucket == "confirmed" and _PROGRESS_UNCERTAIN.search(text):
            return text, "uncertain"
        return text, bucket

    for item in source["confirmed"]:
        text, bucket = normalize(item, "confirmed")
        if bucket == "confirmed":
            confirmed.append(text)
        elif bucket == "uncertain":
            moved.append(text)
    for item in source["uncertain"]:
        text, bucket = normalize(item, "uncertain")
        if bucket == "uncertain":
            uncertain.append(text)

    def unique(items):
        result, seen = [], set()
        for item in items:
            key = item.casefold()
            if key not in seen:
                seen.add(key)
                result.append(item)
            if len(result) == PROGRESS_MAX_ITEMS:
                break
        return tuple(result)

    return ProgressSummary(unique(confirmed), unique(moved + uncertain))


@dataclass(frozen=True)
class Handoff:
    remaining_objective: str
    preserve: tuple[str, ...]
    completion_conditions: tuple[str, ...]

    def __post_init__(self):
        _text(self.remaining_objective, "remaining objective")
        _texts(self.preserve, "preservation constraints")
        _texts(self.completion_conditions, "completion conditions")
        if not self.completion_conditions:
            raise ValueError("Handoff must have observable completion conditions")


@dataclass(frozen=True)
class Fact:
    claim: str
    scope: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class Unknown:
    question: str
    blocks_handoff: bool
    reason: str


@dataclass(frozen=True)
class Plan:
    decision: str
    reason: str
    facts: tuple[Fact, ...] = ()
    unknowns: tuple[Unknown, ...] = ()
    verification: VerificationTask | None = None
    handoff: Handoff | None = None
    progress: ProgressSummary = field(default_factory=ProgressSummary)


@dataclass(frozen=True)
class SlimPlan:
    """Minimal cloud contract; Coordinator expands it into local instructions."""

    decision: str
    objective: str
    completion_conditions: tuple[str, ...] = ()
    completion_status: str = "INCOMPLETE"

    def __post_init__(self):
        if self.decision not in {"VERIFY", "EXECUTE", "FALLBACK"}:
            raise ValueError("Unsupported slim plan decision")
        if self.completion_status not in {"COMPLETE", "INCOMPLETE", "UNVERIFIABLE"}:
            raise ValueError("Unsupported completion status")
        _text(self.objective, "slim plan objective")
        _texts(self.completion_conditions, "slim completion conditions")


@dataclass(frozen=True)
class PlanRequest:
    original_goal: str
    public_constraints: tuple[str, ...]
    history: tuple[OwnAction, ...]
    frames: tuple[PublicFrame, ...]
    current_frame_id: str
    decisions_used: int
    decisions_remaining: int
    verification_rounds_remaining: int
    cloud_calls_remaining_after_this: int
    verification: VerificationTask | None = None
    verification_report: str | None = None
    verification_failed: bool = False
    pre_handoff_frame_id: str | None = None
    total_decisions: int = DEFAULT_TOTAL_DECISIONS

    def __post_init__(self):
        _text(self.original_goal, "original goal")
        _texts(self.public_constraints, "public constraints")
        if type(self.total_decisions) is not int or self.total_decisions < 1:
            raise ValueError("Invalid total decision budget")
        if any(type(v) is not int or not 0 <= v <= self.total_decisions
               for v in (self.decisions_used, self.decisions_remaining)):
            raise ValueError("Invalid decision accounting")
        if self.decisions_used + self.decisions_remaining != self.total_decisions:
            raise ValueError("Decision accounting does not match the configured budget")
        for value in (self.verification_rounds_remaining, self.cloud_calls_remaining_after_this):
            if type(value) is not int or value < 0:
                raise ValueError("Invalid preparation limits")
        if type(self.verification_failed) is not bool:
            raise ValueError("verification_failed must be boolean")
        if not isinstance(self.frames, tuple) or not all(isinstance(f, PublicFrame) for f in self.frames):
            raise ValueError("Explicit public frames are required")
        ids = [f.evidence_id for f in self.frames]
        if len(ids) != len(set(ids)) or self.current_frame_id not in ids:
            raise ValueError("Frame IDs must be unique and include the current frame")
        if self.pre_handoff_frame_id is not None and self.pre_handoff_frame_id not in ids:
            raise ValueError("Historical frame is not supplied")
        if self.pre_handoff_frame_id == self.current_frame_id:
            raise ValueError("The latest observation cannot be the historical pre-handoff frame")
        if not isinstance(self.history, tuple) or not all(isinstance(a, OwnAction) for a in self.history):
            raise ValueError("History must contain only own action records")
        numbers = [a.decision_number for a in self.history]
        if numbers != sorted(set(numbers)) or any(n > self.decisions_used for n in numbers):
            raise ValueError("Invalid own history order or budget")


@dataclass(frozen=True)
class PlanReply:
    plan: Plan | SlimPlan
    usage: dict = field(default_factory=dict)
    elapsed_seconds: float = 0.0


@dataclass(frozen=True)
class LocalInstruction:
    original_goal: str
    public_constraints: tuple[str, ...]
    scope: str
    verification: VerificationTask | None = None
    handoff: Handoff | None = None
    # Fallback may resume the executor's original native conversation instead
    # of starting a new recovery-scoped conversation.
    resume_original: bool = False
    resume_reason: str | None = None
    progress: ProgressSummary = field(default_factory=ProgressSummary)
    condition_status: tuple[tuple[str, str], ...] = ()
    last_action_feedback: dict | None = None
    completion_policy: str = "conditions"
    # None preserves the pre-switch adapter behavior for existing callers.
    condition_updates_enabled: bool | None = None

    def __post_init__(self):
        if self.condition_updates_enabled is not None and type(self.condition_updates_enabled) is not bool:
            raise TypeError("condition_updates_enabled must be boolean or None")
        if self.completion_policy not in {"conditions", "native"}:
            raise ValueError("completion_policy must be conditions or native")
        _text(self.original_goal, "original goal")
        _texts(self.public_constraints, "public constraints")
        if self.scope not in {"original", "verification", "execution", "fallback"}:
            raise ValueError("Invalid local instruction scope")
        if (self.scope == "verification") != (self.verification is not None):
            raise ValueError("Verification scope requires exactly a verification instruction")
        if (self.scope == "execution") != (self.handoff is not None):
            raise ValueError("Execution scope requires exactly a remaining objective")
        if type(self.resume_original) is not bool:
            raise TypeError("resume_original must be a boolean")
        if self.resume_original and self.scope != "fallback":
            raise ValueError("Only fallback scope may resume the original context")
        if self.resume_reason is not None:
            _text(self.resume_reason, "resume reason")
        if not isinstance(self.progress, ProgressSummary):
            raise TypeError("progress must be a ProgressSummary")
        if not isinstance(self.condition_status, tuple):
            raise TypeError("condition_status must be a tuple")
        seen = set()
        for item in self.condition_status:
            if (not isinstance(item, tuple) or len(item) != 2
                    or not isinstance(item[0], str) or not item[0].strip()
                    or item[1] not in RECOVERY_CONDITION_STATUSES):
                raise ValueError("condition_status entries must be (condition, status)")
            if item[0] in seen:
                raise ValueError("condition_status cannot contain duplicates")
            seen.add(item[0])
        if self.last_action_feedback is not None:
            if not isinstance(self.last_action_feedback, dict):
                raise TypeError("last_action_feedback must be a mapping or None")
            json.dumps(self.last_action_feedback, allow_nan=False)
        object.__setattr__(self, "progress", sanitize_progress(self.progress))


@dataclass(frozen=True)
class StepResult:
    kind: str
    scope: str
    decision_number: int | None = None
    action: dict | None = None
    error_type: str | None = None
    error_category: str | None = None
    error_class: str | None = None
    condition_updates: tuple[dict, ...] = ()
