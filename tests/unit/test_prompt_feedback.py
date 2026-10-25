"""Action receipts remain internal rather than bypassing native history policy."""
from dataclasses import replace
import unittest

from handoff_recovery.core.protocols import recovery_task_goal
from handoff_recovery.core.types import Handoff, LocalInstruction, VerificationTask
from handoff_recovery.executor.cloud import _current_context as cloud_context
from handoff_recovery.executor.gui_owl import _current_context as gui_context
from handoff_recovery.executor.phonebuddy import _current_context as phone_context


class PromptFeedbackTests(unittest.TestCase):
    def contexts(self, instruction):
        return (
            ("cloud/ui_voyager/holo", cloud_context(instruction)),
            ("gui_owl", gui_context(instruction)),
            ("phonebuddy", phone_context(instruction, require_completion_check=False)),
        )

    def test_feedback_does_not_change_recovery_task_text(self):
        goal, condition = "Save the requested value.", "The requested value is saved"
        feedback = {"action": "tap", "target": "FEEDBACK_ONLY_TARGET",
                    "result": "rejected", "state_changed": False,
                    "reason": "FEEDBACK_ONLY_REASON"}
        for policy in ("conditions", "native"):
            for scope, final in (("execution", False), ("verification", False),
                                 ("verification", True)):
                instruction = LocalInstruction(
                    goal, (), scope,
                    handoff=Handoff("Finish saving", (), (condition,))
                        if scope == "execution" else None,
                    verification=VerificationTask("Read the saved value", (condition,), (),
                                                  final_confirmation=final)
                        if scope == "verification" else None,
                    condition_status=((condition, "pending"),),
                    last_action_feedback=feedback, completion_policy=policy)
                without = dict(self.contexts(replace(instruction, last_action_feedback=None)))
                for name, context in self.contexts(instruction):
                    with self.subTest(adapter=name, policy=policy, scope=scope, final=final):
                        prompt = recovery_task_goal(goal, context)
                        self.assertEqual(prompt, recovery_task_goal(goal, without[name]))
                        self.assertNotIn("Last action feedback:", prompt)
                        self.assertNotIn("FEEDBACK_ONLY_", prompt)
                        self.assertIn(goal, prompt)
                        self.assertIn("[pending] " + condition, prompt)
                self.assertEqual(instruction.last_action_feedback, feedback)

    def test_original_and_fallback_task_text_stays_verbatim(self):
        goal = "Save the requested value.\nPreserve the existing fields."
        for scope in ("original", "fallback"):
            instruction = LocalInstruction(goal, (), scope,
                last_action_feedback={"action": "tap", "target": "FEEDBACK_ONLY_TARGET"})
            for name, context in self.contexts(instruction):
                with self.subTest(adapter=name, scope=scope):
                    self.assertEqual(recovery_task_goal(goal, context), goal)
