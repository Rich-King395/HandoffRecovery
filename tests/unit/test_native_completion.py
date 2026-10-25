"""Explicit native termination keeps scoring and condition diagnostics separate."""
from dataclasses import replace
import contextlib
import importlib.util
import io
from pathlib import Path
import unittest

from handoff_recovery.core.coordinator import RecoveryCoordinator
from handoff_recovery.core.protocols import recovery_task_goal
from handoff_recovery.core.types import Handoff, LocalInstruction, RecoveryBudget, VerificationTask
from handoff_recovery.executor.cloud import _current_context
from tests.unit.test_coordinator import (
    FakeExecutor, FakePlanner, GOAL, frame, slim_execute_reply, slim_verify_reply,
)


class NativeCompletionTests(unittest.TestCase):
    def coordinator(self, actions, replies, **options):
        executor, planner = FakeExecutor(actions), FakePlanner(replies)
        control = RecoveryCoordinator(task_id="synthetic", original_goal=GOAL,
            executor=executor, planner=planner, **options)
        control.pause_for_handoff()
        control.return_control(frame("returned"))
        return control, executor, planner

    def test_native_done_keeps_pending_conditions_and_does_not_assign_success(self):
        control, executor, planner = self.coordinator([{"action": "done"}], [slim_execute_reply()],
            completion_policy="native", budget=RecoveryBudget(total_decisions=1))
        result = control.step(frame("returned"))
        self.assertEqual((result.kind, result.decision_number), ("task_complete", 1))
        report = control.report()
        self.assertEqual(report["condition_status"][0]["status"], "pending")
        self.assertIsNone(report["task_success"])
        self.assertEqual(report["recovery_features"]["completion_policy"], "native")
        self.assertEqual(len(planner.requests), 1)
        self.assertEqual(executor.requests[0][1].completion_policy, "native")

    def test_default_condition_gate_is_unchanged(self):
        control, _, _ = self.coordinator([{"action": "done"}], [slim_execute_reply()])
        self.assertEqual(control.step(frame("returned")).kind, "invalid_decision")
        self.assertFalse(control.finished)
        self.assertEqual(control.decisions_used, 1)

    def test_native_fail_remains_failure(self):
        control, _, _ = self.coordinator([{"action": "fail"}], [slim_execute_reply()],
                                         completion_policy="native")
        self.assertEqual(control.step(frame("returned")).kind, "agent_failed")

    def test_native_verify_completion_is_a_counted_subtask_then_execute(self):
        control, _, planner = self.coordinator([{"action": "done"}, {"action": "done"}],
            [slim_verify_reply(), slim_execute_reply()], completion_policy="native",
            budget=RecoveryBudget(total_decisions=2))
        self.assertEqual(control.step(frame("returned")).kind, "verification_complete")
        self.assertFalse(control.finished)
        self.assertEqual(control.step(frame("after_verify")).kind, "task_complete")
        self.assertEqual((control.decisions_used, len(planner.requests)), (2, 2))

    def test_native_prompt_requires_no_output_extension_and_default_is_preserved(self):
        instruction = LocalInstruction(GOAL, (), "execution",
            handoff=Handoff("Set the email", (), ("Saved email is visible",)))
        default = recovery_task_goal(GOAL, _current_context(instruction))
        native = recovery_task_goal(GOAL, _current_context(replace(instruction, completion_policy="native")))
        self.assertIn('"condition_updates":', default)
        self.assertNotIn('"condition_updates":', native)
        self.assertIn("existing native completion action", native)
        self.assertIn("Saved email is visible", native)
        self.assertIn(GOAL, native)
        verification = LocalInstruction(GOAL, (), "verification", completion_policy="native",
            verification=VerificationTask("Inspect saved email", ("Saved email is visible",), (),
                                          final_confirmation=True))
        prompt = recovery_task_goal(GOAL, _current_context(verification))
        self.assertIn("existing native completion action", prompt)
        self.assertNotIn("Return done", prompt)

    def test_invalid_policy_and_completion_check_conflict_fail_before_start(self):
        executor = FakeExecutor([], requires_completion_check=True)
        with self.assertRaises(ValueError):
            RecoveryCoordinator(task_id="synthetic", original_goal=GOAL,
                planner=FakePlanner(), executor=executor, completion_policy="native")
        self.assertEqual(executor.starts, [])
        with self.assertRaises(ValueError):
            LocalInstruction(GOAL, (), "original", completion_policy="unknown")

    def test_cli_modes_are_explicit_and_conflicting_completion_checks_are_rejected(self):
        root = Path(__file__).resolve().parents[2]
        live_args = ["--bench-root", "/bench", "--baseline", "/baseline.json", "--device", "synthetic",
                     "--adb", "/adb", "--appium-url", "http://127.0.0.1:4723", "--mode", "no_takeover",
                     "--local-model", "synthetic", "--local-base-url", "http://127.0.0.1:8000/v1"]
        for name, args in (("run_live_smoke", live_args), ("run_recovery_batch", ["--all"])):
            spec = importlib.util.spec_from_file_location("native_test_"+name, root/"scripts"/(name+".py"))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            with self.subTest(script=name):
                self.assertEqual(module.parse_args(args).completion_policy, "conditions")
                self.assertEqual(module.parse_args(args+["--completion-policy", "native"]).completion_policy, "native")
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    module.parse_args(args+["--completion-policy", "native", "--enable-completion-check"])
