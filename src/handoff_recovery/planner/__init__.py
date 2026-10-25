"""Public-evidence recovery planning and an explicit-config HTTP client."""

from .client import OpenAICompatiblePlanner, PlannerConfig, PlannerError
from .contracts import (COMPLETION_STATUSES, OUTPUT_CONTRACT, PLANNER_DECISIONS,
                        SLIM_PLAN_FIELDS, response_format)
from .registry import (create_planner, planner_adapter_names, register_planner)
from .prompt import build_messages
from .validation import PlanValidationError, diagnose_plan_text, parse_plan, validate_plan

__all__ = [
    "OpenAICompatiblePlanner", "PlannerConfig", "PlannerError",
    "PLANNER_DECISIONS", "COMPLETION_STATUSES", "SLIM_PLAN_FIELDS",
    "OUTPUT_CONTRACT", "response_format",
    "create_planner", "planner_adapter_names", "register_planner",
    "PlanValidationError", "build_messages", "diagnose_plan_text", "parse_plan", "validate_plan",
]
