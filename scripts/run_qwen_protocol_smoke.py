"""Call Qwen Planner and GUI Agent once using a recorded public screenshot."""
from __future__ import annotations

import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT.parent / "ResumeBench" / "HandoffBench"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(BENCH))

from handoff_recovery.core.protocols import recovery_task_goal
from handoff_recovery.core.types import OwnAction, PlanRequest, PublicFrame
from handoff_recovery.planner import OpenAICompatiblePlanner, PlannerConfig
from resumebench.models.qwen_model import action_prompt
from resumebench.models.api_method_qwen import QwenConfiguration, request_action


def dotenv(path):
    values = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("\"'")
    return values


def main():
    recovery_env = dotenv(ROOT / ".env")
    bench_env = dotenv(BENCH / ".env")
    screenshot = (ROOT / "results/full-claude-current/"
                  "qwen3.8-max_claude-opus-5_177_30_20261015T152849Z_5ebe7b15/"
                  "task_2002_trial_3/instance_3_takeover/benchmark/observations/000012/screen.png")
    output = ROOT / "results" / "qwen-protocol-smoke-current"
    output.mkdir(parents=True, exist_ok=True)
    frame = PublicFrame.from_png("after_return", screenshot, note="recorded public post-takeover observation")
    history = (OwnAction(1, {"action": "tap", "target": "contact form"},
                         "original", "before_takeover", result="success"),)
    goal = "In Contacts, save a local device contact named ResumeBench Alex with mobile phone 2025550147 and home email alex.resumebench@example.com."
    request = PlanRequest(
        original_goal=goal, public_constraints=(), history=history,
        frames=(frame,), current_frame_id=frame.evidence_id,
        decisions_used=1, decisions_remaining=29,
        verification_rounds_remaining=1, cloud_calls_remaining_after_this=1,
        pre_handoff_frame_id=None, total_decisions=30,
    )
    planner_key = recovery_env.get("RECOVERY_PLANNER_QWEN_API_KEY", "")
    planner = OpenAICompatiblePlanner(PlannerConfig(
        endpoint=recovery_env["RECOVERY_PLANNER_QWEN_ENDPOINT"],
        model=recovery_env.get("RECOVERY_PLANNER_QWEN_MODEL", "qwen3.8-flash"),
        api_key=planner_key, enable_thinking=False,
        response_format_mode="json_schema", use_environment_proxy=True,
    ))
    reply = planner.plan(request)
    plan = reply.plan
    if plan.decision == "VERIFY":
        context = {
            "scope": "verification",
            "verification": {
                "objective": plan.objective,
                "required_evidence": list(plan.completion_conditions),
                "final_confirmation": False,
            },
            "condition_status": [{"condition": item, "status": "pending"}
                                 for item in plan.completion_conditions],
        }
    elif plan.decision == "EXECUTE":
        context = {
            "scope": "execution",
            "handoff": {
                "remaining_objective": plan.objective,
                "completion_conditions": list(plan.completion_conditions),
            },
            "condition_status": [{"condition": item, "status": "pending"}
                                 for item in plan.completion_conditions],
        }
    else:
        context = None
    request_goal = recovery_task_goal(goal, context)
    gui_history = [{"action": "tap", "target": "contact form"}]
    prompt = action_prompt(
        request_goal, frame.width, frame.height, gui_history,
        coordinate_space="normalized_1000",
        completion_conditions=None if context and context.get("scope") == "execution" else (
            list(plan.completion_conditions) if context else None),
        constraints=None, recovery_scope=context.get("scope") if context else None,
    )
    key = bench_env.get("DASHSCOPE_API_KEY", "")
    config = QwenConfiguration(
        api_key=key, model=bench_env.get("QWEN_MODEL", "qwen3.8-max"),
        base_url=bench_env.get("QWEN_BASE_URL", "").rstrip("/"),
        max_output_tokens=int(bench_env.get("QWEN_MAX_OUTPUT_TOKENS", "4096")),
    )
    raw = request_action(
        str(screenshot), request_goal, frame.width, frame.height,
        configuration=config, history=gui_history, completion_conditions=None,
        constraints=None, recovery_scope=context.get("scope") if context else None,
    )
    record = {
        "scope": "real_qwen_protocol_smoke",
        "screenshot": str(screenshot),
        "planner": {"endpoint_host": urlsplit(planner.config.endpoint).hostname or "",
                    "model": planner.config.model, "enable_thinking": False,
                    "output": {"completion_status": plan.completion_status,
                                "decision": plan.decision, "objective": plan.objective,
                                "completion_conditions": list(plan.completion_conditions)},
                    "usage": reply.usage},
        "gui_agent": {"model": config.model,
                      "prompt": prompt,
                      "raw_output": raw},
        "recovery_context": context,
    }
    (output / "record.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                                         encoding="utf-8")
    print(output / "record.json")
    print(json.dumps(record["planner"]["output"], ensure_ascii=False))
    print(raw)


if __name__ == "__main__":
    main()
