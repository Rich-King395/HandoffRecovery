"""Recoverable native rejections never hide transport or integration failures."""
import unittest
from unittest.mock import patch

from handoff_recovery.core.coordinator import RecoveryCoordinator
from handoff_recovery.core.protocols import recovery_task_goal
from handoff_recovery.core.types import Handoff, LocalInstruction, VerificationTask
from handoff_recovery.executor.cloud import _current_context as cloud_context
from handoff_recovery.executor.gui_owl import _current_context as gui_context
from handoff_recovery.executor.phonebuddy import _current_context as phone_context
from handoff_recovery.integrations.handoffbench import HoloBindings, UIVoyagerBindings
from handoff_recovery.executor import (
    ExecutorError, ExecutorSpec, HoloExecutorError, PhoneBuddyExecutorError,
    UIVoyagerExecutorError, create_executor,
)
from tests.unit.test_coordinator import FakeExecutor, FakePlanner, GOAL, frame


class NativeExecutionTests(unittest.TestCase):
    def test_existing_explicit_binding_constructors_keep_their_arguments(self):
        helper = lambda *args, **kwargs: None
        ui = UIVoyagerBindings(object(), helper, helper, helper, helper)
        holo = HoloBindings(object(), helper, helper, helper)
        self.assertIs(ui.validate_action, helper)
        self.assertIs(holo.validate_action, helper)
        self.assertIsNone(ui.validate_configuration)
        self.assertIsNone(holo.validate_configuration)

    def test_native_recovery_information_is_equal_across_model_families(self):
        for scope in ("execution", "verification"):
            instruction = LocalInstruction(GOAL, (), scope,
                handoff=Handoff("Finish the public objective", (), ("The saved value is visible",))
                    if scope == "execution" else None,
                verification=VerificationTask("Read the public saved value", ("The saved value is visible",), ())
                    if scope == "verification" else None,
                condition_status=(("The saved value is visible", "satisfied"),),
                last_action_feedback={"action":"tap", "result":"success", "state_changed":True},
                completion_policy="native")
            expected = recovery_task_goal(GOAL, cloud_context(instruction))
            for name, context in (("gui_owl", gui_context(instruction)),
                    ("phonebuddy", phone_context(instruction, require_completion_check=False)),
                    ("ui_voyager", cloud_context(instruction)), ("holo", cloud_context(instruction))):
                with self.subTest(adapter=name, scope=scope):
                    self.assertEqual(recovery_task_goal(GOAL, context), expected)
                    self.assertEqual(context["last_action_feedback"], instruction.last_action_feedback)
                    context["last_action_feedback"]["result"] = "mutated"
                    self.assertEqual(instruction.last_action_feedback["result"], "success")

    def test_only_marked_native_rejections_continue_and_keep_error_categories(self):
        for cls in (ExecutorError, PhoneBuddyExecutorError, UIVoyagerExecutorError, HoloExecutorError):
            for category, recoverable, expected in (
                ("native_parse_error", True, "invalid_decision"),
                ("action_mapping_error", True, "invalid_decision"),
                ("native_parse_error", False, "agent_error"),
                ("request_error", True, "agent_error"),
                ("integration_error", True, "agent_error"),
                ("history_error", True, "agent_error"),
            ):
                with self.subTest(adapter=cls.__name__, category=category, recoverable=recoverable):
                    error = cls("Synthetic error", category=category, cause_type="ValueError",
                                recoverable=recoverable)
                    executor = FakeExecutor([error, {"action": "done"}])
                    planner = FakePlanner()
                    control = RecoveryCoordinator(task_id="synthetic", original_goal=GOAL,
                        executor=executor, planner=planner, completion_policy="native")
                    result = control.step(frame("first"))
                    self.assertEqual(result.kind, expected)
                    self.assertEqual(control.decisions_used, 1)
                    self.assertEqual(control._history[-1].action["error_category"], category)
                    self.assertEqual(len(executor.requests), 1)
                    self.assertEqual(planner.requests, [])
                    if recoverable and category in {"native_parse_error", "action_mapping_error"}:
                        self.assertFalse(control.finished)
                        self.assertEqual(control.step(frame("second")).kind, "task_complete")

    def test_native_factory_rejects_unsupported_controls_before_configuration_lookup(self):
        for name in ("gui_owl", "phonebuddy", "ui_voyager", "holo"):
            controls = [{"include_recovery_constraints": True}, {"include_recovery_uncertain": True}]
            if name != "phonebuddy":
                controls.append({"enable_completion_check": True})
            for options in controls:
                with self.subTest(adapter=name, options=options), patch(
                        "handoff_recovery.executor.registry.load_local_configuration") as load:
                    spec = ExecutorSpec(agent=name, model="synthetic", base_url="http://model.invalid/v1",
                        configuration_source="handoffbench_provider_validate_configuration", **options)
                    with self.assertRaises(ValueError):
                        create_executor(name, "/bench", spec)
                    load.assert_not_called()

    def test_explicit_configuration_and_dynamic_factories_keep_values(self):
        for name, factory in (("ui_voyager", "make_ui_voyager_executor"), ("holo", "make_holo_executor")):
            with self.subTest(adapter=name), patch(
                    "handoff_recovery.executor.registry.load_local_configuration") as load:
                spec = ExecutorSpec(agent=name, model="explicit", base_url="http://model.invalid/v1",
                    timeout_seconds=57, max_tokens=901, recovery_history_mode="original_snapshot")
                executor = create_executor("handoff_recovery.executor."+name+":"+factory, "/bench", spec)
                self.assertEqual(executor._configuration.max_tokens, 901)
                self.assertEqual(executor._configuration.timeout_seconds, 57)
                self.assertEqual(executor._configuration.model, "explicit")
                self.assertEqual(executor._configuration.recovery_history_mode, "original_snapshot")
                self.assertEqual(executor.descriptor["configuration_source"], "explicit_executor_spec")
                load.assert_not_called()

    def test_native_adapter_cannot_load_a_different_agents_configuration(self):
        with self.assertRaises(ValueError):
            create_executor("ui_voyager", "/bench", ExecutorSpec(agent="holo", model="synthetic"))
