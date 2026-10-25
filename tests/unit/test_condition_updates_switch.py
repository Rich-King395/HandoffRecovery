"""Independent condition claims, native completion and bounded metadata framing."""
from dataclasses import replace
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import unittest

from handoff_recovery.core.coordinator import RecoveryCoordinator
from handoff_recovery.core.protocols import recovery_task_goal
from handoff_recovery.core.types import Handoff, LocalInstruction, VerificationTask
from handoff_recovery.executor.cloud import _current_context
from handoff_recovery.executor.condition_updates import (
    FORMAT_EXTENSION, enabled, experiment_metadata, parse_native_response, request_options,
)
from tests.unit.test_coordinator import FakeExecutor, FakePlanner, GOAL, frame, slim_execute_reply


CLAIMS = [{"condition_id": "c1", "status": "satisfied", "evidence": "The saved value is visible"}]


def strict_native(text):
    if text.strip() != "NATIVE_ACTION":
        raise ValueError("Invalid native action")
    return {"action": "wait"}


class ConditionUpdatesSwitchTests(unittest.TestCase):
    def coordinator(self, actions, *, active, completion="native"):
        executor, planner = FakeExecutor(actions), FakePlanner([slim_execute_reply()])
        control = RecoveryCoordinator(task_id="synthetic", original_goal=GOAL,
            planner=planner, executor=executor, completion_policy=completion,
            condition_updates_enabled=active)
        control.pause_for_handoff()
        control.return_control(frame("returned"))
        return control, executor

    def test_updates_wait_for_successful_execution_and_failures_leave_pending(self):
        for success in (True, False):
            control, executor = self.coordinator(
                [{"action": "wait", "condition_updates": CLAIMS}], active=True)
            result = control.step(frame("first"))
            self.assertEqual((result.kind, control.decisions_used), ("action", 1))
            self.assertNotIn("condition_updates", result.action)
            self.assertEqual(control.report()["condition_status"][0]["status"], "pending")
            control.acknowledge_action(success=success)
            self.assertEqual(control.report()["condition_status"][0]["status"],
                             "satisfied" if success else "pending")
            self.assertIs(executor.requests[0][1].condition_updates_enabled, True)

    def test_switch_and_terminal_gate_are_independent(self):
        for active, completion, claimed, expected in (
            (False, "native", True, "task_complete"),
            (True, "native", False, "task_complete"),
            (True, "conditions", False, "invalid_decision"),
            (True, "conditions", True, "task_complete"),
        ):
            with self.subTest(active=active, completion=completion, claimed=claimed):
                action = {"action": "done", **({"condition_updates": CLAIMS} if claimed else {})}
                control, _ = self.coordinator([action], active=active, completion=completion)
                self.assertEqual(control.step(frame("first")).kind, expected)
                self.assertEqual(control.decisions_used, 1)
                self.assertEqual(control.report()["condition_status"][0]["status"],
                                 "satisfied" if active and claimed else "pending")

    def test_bad_claims_do_not_discard_a_valid_action(self):
        for claims in ([{**CLAIMS[0], "condition_id": "c99"}], CLAIMS * 2,
                       [{**CLAIMS[0], "evidence": ""}], {"invalid_condition_metadata": True}):
            with self.subTest(claims=claims):
                control, _ = self.coordinator([{"action": "wait", "condition_updates": claims}], active=True)
                self.assertEqual(control.step(frame("first")).kind, "action")
                control.acknowledge_action(success=True)
                self.assertEqual(control.report()["condition_status"][0]["status"], "pending")
                self.assertEqual(control.report()["condition_update_counts"]["rejected"], 1)

    def test_prompt_has_stable_ids_and_keeps_native_completion(self):
        conditions = ("The name is saved", "The email is saved")
        instruction = LocalInstruction(GOAL, (), "execution",
            handoff=Handoff("Finish saving", (), conditions), completion_policy="native",
            condition_status=((conditions[0], "satisfied"), (conditions[1], "pending")),
            condition_updates_enabled=True,
            last_action_feedback={"target": "FEEDBACK_ONLY_TARGET"})
        prompt = recovery_task_goal(GOAL, _current_context(instruction))
        self.assertIn("c1 [satisfied]", prompt)
        self.assertIn("c2 [pending]", prompt)
        self.assertIn("diagnostics, not a prerequisite", prompt)
        self.assertNotIn("Do not add condition_updates", prompt)
        self.assertNotIn("FEEDBACK_ONLY_TARGET", prompt)
        legacy = replace(instruction, condition_updates_enabled=None)
        off = replace(instruction, condition_updates_enabled=False)
        self.assertEqual(recovery_task_goal(GOAL, _current_context(off)),
                         recovery_task_goal(GOAL, _current_context(legacy)))
        self.assertEqual(request_options(off), {})
        self.assertEqual(request_options(instruction), {"response_format_extension": FORMAT_EXTENSION})
        verification = LocalInstruction(GOAL, (), "verification", completion_policy="native",
            verification=VerificationTask("Inspect the saved value", (conditions[0],), ()),
            condition_updates_enabled=True)
        self.assertFalse(enabled(verification))
        self.assertEqual(request_options(verification), {})
        checked = replace(off, completion_policy="conditions")
        self.assertIn("include completion_check covering every original condition",
                      recovery_task_goal(GOAL, _current_context(checked, include_completion_check=True)))

    def test_strict_metadata_frame_and_off_path(self):
        raw = "NATIVE_ACTION\n<condition_updates>" + json.dumps(CLAIMS) + "</condition_updates>"
        parsed, native, claims = parse_native_response(raw, strict_native, active=True)
        self.assertEqual(parsed, {"action": "wait"})
        self.assertEqual(native, "NATIVE_ACTION\n")
        self.assertEqual(claims, CLAIMS)
        quoted = [{**CLAIMS[0], "evidence": "Literal </condition_updates> and <tool_call> labels are visible"}]
        quoted_raw = "NATIVE_ACTION\n<condition_updates>" + json.dumps(quoted) + "</condition_updates>"
        self.assertEqual(parse_native_response(quoted_raw, strict_native, active=True)[2], quoted)
        with self.assertRaises(ValueError):
            parse_native_response(raw, strict_native, active=False)
        for tail in ("<condition_updates>{bad}</condition_updates>",
                     '<condition_updates>[{"x":1,"x":2}]</condition_updates>',
                     '<condition_updates>[NaN]</condition_updates>'):
            with self.subTest(tail=tail):
                parsed, _, claims = parse_native_response("NATIVE_ACTION\n" + tail, strict_native, active=True)
                self.assertEqual(parsed, {"action": "wait"})
                self.assertEqual(claims, {"invalid_condition_metadata": True})
        for invalid in (raw + "\nNATIVE_ACTION", raw + "\n<condition_updates>[]</condition_updates>",
                        "NATIVE_ACTION\n<condition_updates>[", "BAD_NATIVE\n<condition_updates>[]</condition_updates>"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_native_response(invalid, strict_native, active=True)

    def test_defaults_and_input_validation(self):
        self.assertEqual(experiment_metadata(None, "gui_owl", "native")["condition_updates"], "disabled")
        self.assertEqual(experiment_metadata(None, "qwen", "conditions")["condition_updates"], "enabled")
        self.assertEqual(experiment_metadata("enabled", "holo", "native")["condition_updates"], "enabled")
        for value in ("enabled", 1, []):
            with self.subTest(value=value), self.assertRaises(TypeError):
                LocalInstruction(GOAL, (), "original", condition_updates_enabled=value)

    def test_script_cli_accepts_independent_switch_without_device_access(self):
        scripts = Path(__file__).resolve().parents[2] / "scripts"
        for name in ("run_live_smoke", "run_recovery_batch"):
            spec = importlib.util.spec_from_file_location("condition_switch_" + name, scripts / (name + ".py"))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            base = (["--bench-root", "/unused", "--baseline", "/unused.json", "--device", "synthetic",
                     "--adb", "/adb", "--appium-url", "http://appium.invalid", "--instances", "3",
                     "--local-model", "synthetic-model", "--local-base-url", "http://model.invalid/v1",
                     "--credentials-file", "/unused.env", "--planner-endpoint", "http://planner.invalid/v1/chat/completions",
                     "--planner-model", "synthetic-planner"]
                    if name == "run_live_smoke" else ["--tasks", "3"])
            for mode in ("enabled", "disabled"):
                with self.subTest(script=name, mode=mode):
                    args = module.parse_args(base + ["--condition-updates", mode, "--completion-policy", "native"])
                    self.assertEqual((args.condition_updates, args.completion_policy), (mode, "native"))
            self.assertIsNone(module.parse_args(base).condition_updates)
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                module.parse_args(base + ["--condition-updates", "invalid"])
            if name == "run_live_smoke":
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    module.parse_args(base + ["--condition-updates", "disabled"])
