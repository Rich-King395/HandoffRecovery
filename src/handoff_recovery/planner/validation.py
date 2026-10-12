"""Structural and provenance checks, not a proof of visual correctness."""

from dataclasses import asdict
import json
from typing import Any

from handoff_recovery.core.types import (
    ConditionalAction, Fact, Handoff, Plan, PlanRequest, SlimPlan, Unknown, VerificationTask,
)


class PlanValidationError(ValueError):
    """A plan violates the public output contract (messages omit raw content)."""

    _CATEGORIES = (
        ("Duplicate JSON field", "duplicate_field"),
        ("Non-finite JSON number", "non_finite_number"),
        ("Expected one strict JSON object", "invalid_json"),
        ("Planner output is not bounded text", "response_size"),
        ("Missing, unexpected, or invalid object fields", "object_shape"),
        ("Expected an execution handoff object", "handoff_type"),
        ("Missing, unexpected, or invalid handoff fields", "handoff_shape"),
        ("Expected a conditional action array", "conditional_actions_type"),
        ("Expected a nonempty text field", "text_field"),
        ("Expected a text array", "text_array"),
        ("Expected an array", "array_type"),
        ("Unsupported planner decision", "decision_value"),
        ("Ambiguous public evidence IDs", "evidence_id_ambiguity"),
        ("Current frame is missing", "missing_current_frame"),
        ("Pre-handoff frame is missing", "missing_pre_handoff_frame"),
        ("Unsupported fact scope", "fact_scope"),
        ("Fact cites unavailable public evidence", "evidence_id_unavailable"),
        ("Current facts require post-return image evidence", "current_fact_evidence"),
        ("blocks_handoff must be boolean", "unknown_flag_type"),
        ("VERIFY cannot supply a final handoff", "verify_handoff_conflict"),
        ("Insufficient remaining verification budget", "verify_budget"),
        ("VERIFY must identify a blocking unknown", "verify_unknown_required"),
        ("EXECUTE cannot request verification", "execute_verification_conflict"),
        ("EXECUTE has an unresolved blocking unknown", "execute_blocking_unknown"),
        ("FALLBACK cannot supply new instructions", "fallback_instruction_conflict"),
        ("Expected a Plan", "plan_type"),
        ("Invalid plan structure", "plan_structure"),
    )

    def __init__(self, message: str, *, category: str | None = None):
        # Categories are bounded identifiers; raw messages remain local only.
        resolved = category
        if resolved is None:
            resolved = next((value for marker, value in self._CATEGORIES if message == marker),
                            "contract_violation")
        if not isinstance(resolved, str) or not resolved.isidentifier():
            resolved = "contract_violation"
        self.category = resolved
        super().__init__(message)


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PlanValidationError("Duplicate JSON field")
        result[key] = value
    return result


def _constant(_: str) -> None:
    raise PlanValidationError("Non-finite JSON number")


def strict_json(text: str) -> Any:
    try:
        return json.loads(text, object_pairs_hook=_object, parse_constant=_constant)
    except PlanValidationError:
        # Preserve the specific bounded category raised by the structural hooks.
        raise
    except (ValueError, TypeError, RecursionError):
        raise PlanValidationError("Expected one strict JSON object") from None


def _fields(value: Any, names: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != names:
        raise PlanValidationError("Missing, unexpected, or invalid object fields")
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanValidationError("Expected a nonempty text field")
    return value


def _texts(value: Any, *, required: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or (required and not value):
        raise PlanValidationError("Expected a text array")
    return tuple(_text(item) for item in value)


def _array(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise PlanValidationError("Expected an array")
    return value


def _handoff(value: Any) -> Handoff:
    if not isinstance(value, dict):
        raise PlanValidationError("Expected an execution handoff object")
    required = {"remaining_objective", "preserve", "completion_conditions"}
    optional = {"inspect_before_action", "conditional_actions"}
    if set(value) - required - optional or not required <= set(value):
        raise PlanValidationError("Missing, unexpected, or invalid handoff fields")
    inspections = _texts(value.get("inspect_before_action", []))
    conditional = value.get("conditional_actions", [])
    if not isinstance(conditional, list):
        raise PlanValidationError("Expected a conditional action array")
    actions = []
    for item in conditional:
        item = _fields(item, {"when", "then"})
        actions.append(( _text(item["when"]), _text(item["then"])))
    return Handoff(
        _text(value["remaining_objective"]),
        _texts(value["preserve"]),
        _texts(value["completion_conditions"], required=True),
        inspections,
        tuple(ConditionalAction(when, then) for when, then in actions),
    )


def _parse(value: Any, request: PlanRequest) -> Plan:
    allowed = {"decision", "reason", "facts", "unknowns", "verification", "handoff",
               "use_prior_progress"}
    required = {"decision", "reason", "facts", "unknowns", "verification", "handoff"}
    if (not isinstance(value, dict) or not required <= set(value)
            or not set(value) <= allowed):
        raise PlanValidationError("Missing, unexpected, or invalid object fields")
    decision = _text(value["decision"])
    if decision not in {"VERIFY", "EXECUTE", "FALLBACK"}:
        raise PlanValidationError("Unsupported planner decision")
    reason = _text(value["reason"])
    frame_ids = {frame.evidence_id for frame in request.frames}
    action_ids = {f"action_{action.decision_number}" for action in request.history}
    if len(frame_ids) != len(request.frames) or frame_ids & action_ids:
        raise PlanValidationError("Ambiguous public evidence IDs")
    if request.current_frame_id not in frame_ids:
        raise PlanValidationError("Current frame is missing")
    historical_id = request.pre_handoff_frame_id
    if historical_id is not None and historical_id not in frame_ids:
        raise PlanValidationError("Pre-handoff frame is missing")
    current_frame_ids = frame_ids - {historical_id}
    facts = []
    for item in _array(value["facts"]):
        item = _fields(item, {"claim", "scope", "evidence_ids"})
        scope = _text(item["scope"])
        if scope not in {"current", "historical"}:
            raise PlanValidationError("Unsupported fact scope")
        evidence = _texts(item["evidence_ids"], required=True)
        if not set(evidence) <= frame_ids | action_ids:
            raise PlanValidationError("Fact cites unavailable public evidence")
        if scope == "current" and not set(evidence) & current_frame_ids:
            raise PlanValidationError("Current facts require post-return image evidence")
        facts.append(Fact(_text(item["claim"]), scope, evidence))
    unknowns = []
    for item in _array(value["unknowns"]):
        item = _fields(item, {"question", "blocks_handoff", "reason"})
        if type(item["blocks_handoff"]) is not bool:
            raise PlanValidationError("blocks_handoff must be boolean")
        unknowns.append(Unknown(_text(item["question"]), item["blocks_handoff"], _text(item["reason"])))
    verification = None
    handoff = None
    if decision == "VERIFY":
        if value["handoff"] is not None:
            raise PlanValidationError("VERIFY cannot supply a final handoff")
        if (request.verification_rounds_remaining < 1
                or request.cloud_calls_remaining_after_this < 1
                or request.decisions_remaining < 2):
            raise PlanValidationError("Insufficient remaining verification budget")
        if not any(item.blocks_handoff for item in unknowns):
            raise PlanValidationError("VERIFY must identify a blocking unknown")
        item = _fields(value["verification"], {"objective", "required_evidence", "constraints"})
        verification = VerificationTask(
            _text(item["objective"]), _texts(item["required_evidence"], required=True),
            _texts(item["constraints"], required=True),
        )
    elif decision == "EXECUTE":
        if value["verification"] is not None:
            raise PlanValidationError("EXECUTE cannot request verification")
        if any(item.blocks_handoff for item in unknowns):
            raise PlanValidationError("EXECUTE has an unresolved blocking unknown")
        handoff = _handoff(value["handoff"])
    elif value["verification"] is not None or value["handoff"] is not None:
        raise PlanValidationError("FALLBACK cannot supply new instructions")
    use_prior_progress = value.get("use_prior_progress", False)
    if type(use_prior_progress) is not bool:
        raise PlanValidationError("Missing, unexpected, or invalid object fields")
    return Plan(decision, reason, tuple(facts), tuple(unknowns), verification, handoff,
                use_prior_progress)


def _parse_slim(value: Any, request: PlanRequest) -> SlimPlan:
    """Parse the minimal cloud contract; evidence and branch semantics stay local."""
    allowed = {"decision", "objective", "completion_conditions", "constraints",
               "use_prior_progress"}
    required = {"decision", "objective", "completion_conditions", "constraints"}
    if (not isinstance(value, dict) or not required <= set(value)
            or not set(value) <= allowed):
        raise PlanValidationError("Missing, unexpected, or invalid object fields")
    decision = _text(value["decision"])
    if decision not in {"VERIFY", "EXECUTE", "FALLBACK"}:
        raise PlanValidationError("Unsupported planner decision")
    objective = _text(value["objective"])
    completion = _texts(value["completion_conditions"])
    constraints = _texts(value["constraints"])
    if decision in {"VERIFY", "EXECUTE"} and not completion:
        raise PlanValidationError("Expected a text array")
    if decision == "VERIFY" and (
            request.verification_rounds_remaining < 1
            or request.cloud_calls_remaining_after_this < 1
            or request.decisions_remaining < 2):
        raise PlanValidationError("Insufficient remaining verification budget")
    use_prior_progress = value.get("use_prior_progress", False)
    if type(use_prior_progress) is not bool:
        raise PlanValidationError("Missing, unexpected, or invalid object fields")
    return SlimPlan(decision, objective, completion, constraints, use_prior_progress)


def parse_plan(text: str, request: PlanRequest) -> Plan | SlimPlan:
    """Accept the SlimPlan contract and retain the legacy Plan contract.

    Current claims need an image other than the pre-handoff image. Multiple
    post-return images can support inspected fields, but this cannot establish
    persistence, truth, or that the model retained every original requirement.
    """
    if not isinstance(text, str) or len(text) > 200_000:
        raise PlanValidationError("Planner output is not bounded text")
    value = strict_json(text)
    if (isinstance(value, dict)
            and {"decision", "objective", "completion_conditions", "constraints"} <= set(value)
            and set(value) <= {"decision", "objective", "completion_conditions", "constraints",
                               "use_prior_progress"}):
        return _parse_slim(value, request)
    return _parse(value, request)


def diagnose_plan_text(text: str) -> dict[str, Any]:
    """Return bounded structural diagnostics for an opt-in local debug artifact."""
    result: dict[str, Any] = {"text_type": type(text).__name__}
    if not isinstance(text, str):
        return result
    result["text_length"] = len(text)
    try:
        value = strict_json(text)
    except PlanValidationError as exc:
        result["parse_error_category"] = exc.category
        return result
    result["root_type"] = type(value).__name__
    if not isinstance(value, dict):
        return result
    expected = {"decision", "reason", "facts", "unknowns", "verification", "handoff"}
    result["root_keys"] = sorted(value)
    result["root_missing"] = sorted(expected - set(value))
    result["root_extra"] = sorted(set(value) - expected)
    for name, keys in (("facts", {"claim", "scope", "evidence_ids"}),
                       ("unknowns", {"question", "blocks_handoff", "reason"})):
        items = value.get(name)
        if isinstance(items, list):
            for index, item in enumerate(items):
                if isinstance(item, dict) and set(item) != keys:
                    result["nested_shape"] = {
                        "location": f"{name}[{index}]", "keys": sorted(item),
                        "missing": sorted(keys - set(item)),
                        "extra": sorted(set(item) - keys),
                    }
                    return result
    handoff = value.get("handoff")
    if isinstance(handoff, dict):
        keys = {"remaining_objective", "preserve", "completion_conditions",
                "inspect_before_action", "conditional_actions"}
        required = {"remaining_objective", "preserve", "completion_conditions"}
        if (set(handoff) - keys) or not required <= set(handoff):
            result["nested_shape"] = {"location": "handoff", "keys": sorted(handoff),
                                       "missing": sorted(keys - set(handoff)),
                                       "extra": sorted(set(handoff) - keys)}
            return result
        actions = handoff.get("conditional_actions")
        if isinstance(actions, list):
            for index, item in enumerate(actions):
                if isinstance(item, dict) and set(item) != {"when", "then"}:
                    result["nested_shape"] = {"location": f"handoff.conditional_actions[{index}]",
                                               "keys": sorted(item),
                                               "missing": sorted({"when", "then"} - set(item)),
                                               "extra": sorted(set(item) - {"when", "then"})}
                    return result
    return result


def validate_plan(plan: Plan | SlimPlan, request: PlanRequest) -> Plan | SlimPlan:
    """Apply the same checks to scripted or otherwise in-process plans."""
    if not isinstance(plan, (Plan, SlimPlan)):
        raise PlanValidationError("Expected a Plan")
    try:
        # JSON normalizes frozen dataclass tuples to the public wire arrays.
        value = strict_json(json.dumps(asdict(plan), allow_nan=False))
    except (TypeError, ValueError, RecursionError):
        raise PlanValidationError("Invalid plan structure") from None
    return _parse_slim(value, request) if isinstance(plan, SlimPlan) else _parse(value, request)
