"""Adapter registry tests; no model service or Benchmark checkout is used."""
from __future__ import annotations

import unittest

from handoff_recovery.core.types import PlanReply
from handoff_recovery.core import ExecutorContractError, validate_executor
from handoff_recovery.executor import (ExecutorSpec, GUIOwlConfiguration, GUIOwlExecutor,
                                       PhoneBuddyConfiguration, PhoneBuddyExecutor,
                                       create_executor, register_executor)
from handoff_recovery.planner import PlannerConfig, create_planner, register_planner


class _Planner:
    def __init__(self, config):
        self.config = config

    def plan(self, request):
        raise AssertionError("test Planner must not be called")


class _Executor:
    def __init__(self, bench, spec):
        self.bench = bench
        self.spec = spec

    def start_task(self, task_id, original_goal):
        pass

    def decide(self, frame, instruction):
        raise AssertionError("test Executor must not be called")


class _MissingDecision:
    def start_task(self, task_id, original_goal):
        pass


class _InvalidOptional:
    requires_completion_check = "yes"

    def start_task(self, task_id, original_goal):
        pass

    def decide(self, frame, instruction):
        return {"action": "done"}


class RegistryTests(unittest.TestCase):
    def test_custom_factories_receive_explicit_normalized_config(self):
        register_planner("test_planner", _Planner)
        planner = create_planner("test_planner", PlannerConfig(
            endpoint="https://planner.example.test/v1/chat/completions",
            model="test-model", api_key="test-key"))
        self.assertIsInstance(planner, _Planner)
        self.assertEqual(planner.config.model, "test-model")

        register_executor("test_executor", _Executor)
        executor = create_executor("test_executor", "/bench", ExecutorSpec(
            agent="custom", model="agent-model", base_url="http://127.0.0.1:9000/v1"))
        self.assertIsInstance(executor, _Executor)
        self.assertEqual(executor.bench, "/bench")
        self.assertEqual(executor.spec.base_url, "http://127.0.0.1:9000/v1")

    def test_unknown_adapter_is_rejected(self):
        config = PlannerConfig(endpoint="https://planner.example.test/v1/chat/completions",
                               model="test-model", api_key="test-key")
        with self.assertRaises(ValueError):
            create_planner("missing", config)
        with self.assertRaises(ValueError):
            create_executor("missing", "/bench", ExecutorSpec(
                agent="custom", model="agent-model", base_url="http://127.0.0.1:9000/v1"))

    def test_runtime_contract_rejects_missing_required_or_invalid_optional_members(self):
        with self.assertRaises(ExecutorContractError):
            validate_executor(_MissingDecision())
        with self.assertRaises(ExecutorContractError):
            validate_executor(_InvalidOptional())

    def test_builtin_descriptors_are_json_safe_and_do_not_expose_keys(self):
        phonebuddy = PhoneBuddyExecutor(
            "/bench", PhoneBuddyConfiguration(api_key="private-key"))
        gui_owl = GUIOwlExecutor(
            "/bench", GUIOwlConfiguration(api_key="private-key"))
        for executor, name in ((phonebuddy, "phonebuddy"), (gui_owl, "gui_owl")):
            validate_executor(executor)
            self.assertEqual(executor.descriptor["adapter"], name)
            self.assertNotIn("private-key", repr(executor.descriptor))
