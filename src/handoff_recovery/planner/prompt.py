"""Render only fields explicitly admitted by the public planning contract."""

import base64
from importlib.resources import files
import json
from typing import Any

from handoff_recovery.core.types import PlanRequest
from .contracts import OUTPUT_CONTRACT


def build_messages(request: PlanRequest) -> list[dict[str, Any]]:
    """Send PNG bytes as data URLs; no paths, arbitrary metadata, or environment."""
    verification = request.verification
    public = {
        "original_task_verbatim": request.original_goal,
        "own_action_history": [
            {"action": item.action.get("action", "unknown"),
             "target": item.action.get("target", "")}
            for item in request.history if item.result == "success"
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
            "executor_report_unverified": request.verification_report,
        },
        "verification_failed": request.verification_failed,
        "output_field_contract": OUTPUT_CONTRACT,
        "branch_constraints": (
            "Return exactly the four required fields. completion_status must be COMPLETE, INCOMPLETE, or UNVERIFIABLE. "
            "COMPLETE must choose VERIFY; UNVERIFIABLE cannot choose EXECUTE. decision must be EXECUTE, VERIFY, or FALLBACK. "
            "For EXECUTE, objective describes all remaining work and completion_conditions describe observable "
            "conditions covering the ORIGINAL task's final state, including its required values. If the task appears complete, return EXECUTE with an objective to confirm the final state "
            "and finish; do not use FALLBACK for a completed task. For VERIFY, objective describes one bounded "
            "read-only check and completion_conditions describe only the evidence that must be observed. "
            "A COMPLETE assessment uses one final read-only confirmation. If verification_failed is true, "
            "completion_status cannot be COMPLETE and the decision must be EXECUTE or FALLBACK; do not request another VERIFY. Use FALLBACK only when reliable guidance "
            "cannot be formed; its objective must say to resume the original task. The current screenshot "
            "is authoritative. Return no extra fields, evidence IDs, reasons, Markdown, or surrounding commentary."
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
