"""Shared wire-level Planner contract definitions.

Keeping these definitions in one module prevents the transport, prompt and
validator from drifting apart when another model endpoint is added.
"""
from __future__ import annotations

from typing import Any


PLANNER_DECISIONS = ("VERIFY", "EXECUTE", "FALLBACK")
COMPLETION_STATUSES = ("COMPLETE", "INCOMPLETE", "UNVERIFIABLE")
SLIM_PLAN_FIELDS = frozenset({
    "decision", "completion_status", "objective", "completion_conditions",
})
SLIM_PLAN_REQUIRED_FIELDS = (
    "completion_status", "decision", "objective", "completion_conditions",
)

OUTPUT_CONTRACT = {
    "decision": "VERIFY | EXECUTE | FALLBACK",
    "completion_status": "COMPLETE | INCOMPLETE | UNVERIFIABLE assessment of the original task",
    "objective": "one complete remaining task or bounded verification task",
    "completion_conditions": ["observable conditions for finishing the objective"],
}


def response_format() -> dict[str, Any]:
    """Return the strict JSON Schema envelope used by compatible endpoints."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "handoff_recovery_plan",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "decision": {"type": "string", "enum": list(PLANNER_DECISIONS)},
                    "completion_status": {
                        "type": "string", "enum": list(COMPLETION_STATUSES),
                    },
                    "objective": {"type": "string"},
                    "completion_conditions": {
                        "type": "array", "items": {"type": "string"},
                    },
                },
                "required": list(SLIM_PLAN_REQUIRED_FIELDS),
                "additionalProperties": False,
            },
        },
    }
