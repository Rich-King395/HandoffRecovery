"""Local decision adapters; importing this package contacts no service."""

from .gui_owl import ExecutorError, GUIOwlConfiguration, GUIOwlExecutor
from .phonebuddy import PhoneBuddyConfiguration, PhoneBuddyExecutor, PhoneBuddyExecutorError
from .cloud import CloudExecutor, CloudExecutorConfiguration, CloudExecutorError
from .holo import HoloConfiguration, HoloExecutor, HoloExecutorError
from .ui_voyager import UIVoyagerConfiguration, UIVoyagerExecutor, UIVoyagerExecutorError
from .registry import (ExecutorSpec, create_executor, executor_adapter_names, register_executor)
from handoff_recovery.core.protocols import (ExecutorContractError, validate_executor)

__all__ = [
    "ExecutorError", "GUIOwlConfiguration", "GUIOwlExecutor",
    "CloudExecutor", "CloudExecutorConfiguration", "CloudExecutorError",
    "HoloConfiguration", "HoloExecutor", "HoloExecutorError",
    "UIVoyagerConfiguration", "UIVoyagerExecutor", "UIVoyagerExecutorError",
    "PhoneBuddyConfiguration", "PhoneBuddyExecutor", "PhoneBuddyExecutorError",
    "ExecutorSpec", "create_executor", "executor_adapter_names", "register_executor",
    "ExecutorContractError", "validate_executor",
]
