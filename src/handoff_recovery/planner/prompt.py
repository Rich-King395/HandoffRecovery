"""Render only fields explicitly admitted by the public planning contract."""

import base64
from importlib.resources import files
import json
from typing import Any

from handoff_recovery.core.types import PlanRequest


OUTPUT_CONTRACT = {
    "decision": "VERIFY | EXECUTE | FALLBACK",
    "objective": "one complete remaining task or bounded verification task",
    "completion_conditions": ["observable conditions for finishing the objective"],
    "constraints": ["public preservation and safety constraints"],
}


def build_messages(request: PlanRequest) -> list[dict[str, Any]]:
    """Send PNG bytes as data URLs; no paths, arbitrary metadata, or environment."""
    verification = request.verification
    public = {
        "original_task_verbatim": request.original_goal,
        "public_constraints": list(request.public_constraints),
        "control_event": "Control has returned. No takeover-period actions or images are available.",
        "budget": {
            "total_local_decisions": request.total_decisions,
            "decisions_used": request.decisions_used,
            "decisions_remaining": request.decisions_remaining,
            "verification_rounds_remaining": request.verification_rounds_remaining,
            "cloud_calls_remaining_after_this": request.cloud_calls_remaining_after_this,
        },
        "own_action_history": [
            {"evidence_id": f"action_{item.decision_number}", "decision_number": item.decision_number,
             "action_attempt": item.action, "scope": item.scope, "observed_frame_before_action": item.frame_id}
            for item in request.history
        ],
        "images": [
            {"evidence_id": frame.evidence_id, "width": frame.width, "height": frame.height,
             "observation_note": frame.note, "is_latest_observation": frame.evidence_id == request.current_frame_id,
             "is_pre_handoff": frame.evidence_id == request.pre_handoff_frame_id}
            for frame in request.frames
        ],
        "current_frame_id": request.current_frame_id,
        "pre_handoff_frame_id": request.pre_handoff_frame_id,
        "verification_update": None if verification is None else {
            "requested_objective": verification.objective,
            "required_evidence": list(verification.required_evidence),
            "constraints": list(verification.constraints),
            "executor_report_unverified": request.verification_report,
        },
        "output_field_contract": OUTPUT_CONTRACT,
        "branch_constraints": (
            "Return exactly these four fields with no extras. decision must be EXECUTE, VERIFY, or FALLBACK. "
            "For EXECUTE, objective describes all remaining work and completion_conditions describe observable "
            "final state. If the task appears complete, return EXECUTE with an objective to confirm the final state "
            "and finish; do not use FALLBACK for a completed task. For VERIFY, objective describes one bounded "
            "read-only check and completion_conditions describe the evidence that must be observed. constraints "
            "must preserve the original task and prevent unsafe changes. Use FALLBACK only when reliable guidance "
            "cannot be formed; its objective must say to resume the original task and both arrays may be empty. "
            "Do not return facts, unknowns, evidence IDs, reasons, Markdown, or surrounding commentary."
        ),
    }
    content: list[dict[str, Any]] = [{"type": "text", "text": json.dumps(public, ensure_ascii=False, allow_nan=False)}]
    for frame in request.frames:
        content.extend([
            {"type": "text", "text": f"Public image evidence: {frame.evidence_id}"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(frame.png).decode("ascii")}},
        ])
    return [
        {"role": "system", "content": files(__package__).joinpath("system_prompt.txt").read_text(encoding="utf-8")},
        {"role": "user", "content": content},
    ]
