"""Real HandoffBench runner + recovery bridge, with synthetic devices/models.

Opt in using HANDOFFBENCH_ROOT. No live device, credential, or endpoint is used.
The runner, takeover lifecycle, budgets and scoring logic remain real.
"""
from __future__ import annotations

from collections import deque
from copy import deepcopy
import json
import os
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
import zlib

from handoff_recovery.core.types import Handoff, Plan, PlanReply, Unknown, VerificationTask
from handoff_recovery.executor import GUIOwlConfiguration, GUIOwlExecutor
from handoff_recovery.integrations.handoffbench import HandoffRecoveryAgent


def png_bytes(color=64):
    def chunk(kind, payload):
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 56, 112, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\x00" + bytes([color, 32, 32]) * 56) * 112))
            + chunk(b"IEND", b""))


def execute_plan():
    return PlanReply(Plan("EXECUTE", "The remaining public goal is clear.",
        handoff=Handoff("Complete the unchanged original task from the current page.",
                        ("Keep unrelated user data.",), ("The original requested final setting is visible.",))))


def verify_plan():
    return PlanReply(Plan("VERIFY", "The saved value is not visible.",
        unknowns=(Unknown("What is the saved value?", True, "The current page does not show it."),),
        verification=VerificationTask("Read the saved setting on its details page.",
                                      ("The visible saved value.",), ("Do not edit data.",))))


class Planner:
    def __init__(self, replies=()):
        self.replies = deque(replies)
        self.calls = []

    def plan(self, request):
        self.calls.append(request)
        reply = self.replies.popleft()
        if isinstance(reply, Exception):
            raise reply
        return reply


class Executor:
    def __init__(self, actions):
        self.actions = deque(actions)
        self.calls = []
        self.starts = []

    def start_task(self, task_id, goal):
        self.starts.append((task_id, goal))

    def decide(self, frame, instruction):
        self.calls.append((frame, instruction))
        return deepcopy(self.actions.popleft())


class Device:
    def __init__(self, fail_action=False):
        self.state = "initial"
        self.public_actions = []
        self.user_actions = []
        self.fail_action = fail_action

    def capture(self, directory, deadline=None):
        directory = Path(directory)
        directory.mkdir(parents=True)
        path = directory / "screen.png"
        path.write_bytes(png_bytes(32 if self.state == "initial" else 128))
        return {"xml": f'<hierarchy marker="{self.state}"/>', "screenshot": str(path),
                "width": 56, "height": 112, "private_sentinel": "MUST_NOT_REACH_MODELS"}

    def execute(self, action, deadline=None):
        self.public_actions.append(deepcopy(action))
        return {"success": not self.fail_action}

    def execute_control(self, action, deadline=None):
        self.user_actions.append(deepcopy(action))
        self.state = "returned"
        return {"success": True}


class Checker:
    def __init__(self, *, prefix_decisions=0, goal_status="true"):
        self.prefix_decisions = prefix_decisions
        self.entry_polls = 0
        self.goal_status = goal_status
        self.calls = []

    def check(self, name, state):
        self.calls.append(name)
        if name == "entry":
            matched = self.entry_polls >= self.prefix_decisions
            self.entry_polls += 1
            return {"status": "true" if matched else "false"}
        if name == "handoff":
            return {"status": "true" if "returned" in state["xml"] else "false"}
        return {"status": self.goal_status if name == "goal" else "true"}


@unittest.skipUnless(os.environ.get("HANDOFFBENCH_ROOT"), "Set HANDOFFBENCH_ROOT for real-runner integration")
class RunnerBridgeTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(os.environ["HANDOFFBENCH_ROOT"]).resolve()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.task = {"id": 987654, "app": "synthetic.settings", "description": "Set the visible test option.",
                     "visible_contract": "Keep unrelated user data.", "rule_key": "goal",
                     "environment": {"width": 56, "height": 112}}
        self.scenario = {"id": "PRIVATE_SCENARIO", "task_id": self.task["id"], "entry_check": "entry",
                         "flow_id": "PRIVATE_FLOW", "prefix_max_decisions": 20,
                         "resume_max_decisions": 20, "resume_checks": ["resume_guard"]}
        self.flow = {"timeout_seconds": 2, "before_check": "entry", "after_check": "handoff",
                     "initial_state": "entry", "max_transitions": 2,
                     "locators": {"button": {"by": "accessibility_id", "value": "Synthetic"}},
                     "states": {"entry": {"check": "entry", "action": {"op": "click", "target": "button"}, "next": "end"},
                                "end": {"check": "handoff", "terminal": True, "timeout_seconds": 0.02}}}

    def run_case(self, actions, replies=(), *, mode="takeover", prefix_decisions=0,
                 goal_status="true", executor=None, fail_action=False, **options):
        planner = Planner(replies)
        executor = executor or Executor(actions)
        bridge = HandoffRecoveryAgent(bench_path=self.root, planner=planner, executor=executor)
        from resumebench.core.runner import run_task
        device = Device(fail_action=fail_action)
        checker = Checker(prefix_decisions=prefix_decisions, goal_status=goal_status)
        kwargs = {"scenario": self.scenario, "flow": self.flow} if mode == "takeover" else {}
        result = run_task(task=self.task, mode=mode, device=device, agent=bridge, checker=checker,
                          run_dir=Path(self.directory.name) / "run", settle_seconds=0,
                          **kwargs, **options)
        self.assertEqual(sum(result["counts"]["decisions"].values()), bridge.coordinator.decisions_used)
        return result, bridge, planner, executor, device, checker

    def test_no_takeover_remains_local_and_score_is_independent(self):
        result, bridge, planner, executor, device, _ = self.run_case(
            [{"action": "done"}], mode="no_takeover", goal_status="false")
        self.assertEqual(result["termination_reason"], "agent_done")
        self.assertIs(result["task_success"], False)
        self.assertIsNone(bridge.report()["task_success"])
        self.assertEqual(len(executor.calls), 1)
        self.assertFalse(planner.calls)
        self.assertFalse(device.public_actions)

    def test_initial_takeover_without_prefix_has_no_invented_pre_image(self):
        result, bridge, planner, executor, _, _ = self.run_case(
            [{"action": "done"}], [execute_plan()])
        self.assertEqual(result["takeover_status"], "VALID")
        self.assertEqual(result["counts"]["decisions"], {"task": 0, "prefix": 0, "resume": 1})
        self.assertIsNone(planner.calls[0].pre_handoff_frame_id)
        self.assertEqual(planner.calls[0].history, ())
        self.assertEqual(executor.calls[0][1].scope, "execution")
        self.assertEqual(bridge.report()["cloud_calls_after_handoff"], 0)
        self.assertNotIn("PRIVATE_", repr(planner.calls))
        self.assertNotIn("MUST_NOT_REACH_MODELS", repr(planner.calls))

    def test_verification_completion_is_counted_but_not_sent_to_device_or_terminal(self):
        result, bridge, planner, executor, device, checker = self.run_case(
            [{"action": "wait"}, {"action": "done"}, {"action": "done"}],
            [verify_plan(), execute_plan()])
        self.assertEqual(result["termination_reason"], "agent_done")
        self.assertIs(result["task_success"], True)
        self.assertEqual(result["counts"]["decisions"]["resume"], 3)
        self.assertEqual(len(device.public_actions), 1)
        self.assertEqual(checker.calls.count("goal"), 1)
        self.assertEqual([request[1].scope for request in executor.calls],
                         ["verification", "verification", "execution"])
        self.assertEqual(len(planner.calls), 2)
        self.assertEqual(planner.calls[1].decisions_used, 2)
        self.assertEqual(bridge.report()["cloud_calls_after_handoff"], 0)

    def test_prefix_and_resume_preserve_goal_history_and_budget(self):
        result, bridge, planner, executor, _, _ = self.run_case(
            [{"action": "wait"}, {"action": "done"}], [execute_plan()], prefix_decisions=1)
        self.assertEqual(result["counts"]["decisions"], {"task": 0, "prefix": 1, "resume": 1})
        request = planner.calls[0]
        self.assertEqual((request.decisions_used, request.decisions_remaining), (1, 19))
        self.assertEqual(request.pre_handoff_frame_id, executor.calls[0][0].evidence_id)
        self.assertEqual(request.history[0].action["action"], "wait")
        self.assertEqual(len(executor.starts), 1)
        self.assertEqual(bridge.report()["budget"]["used"], 2)

    def test_deferred_prefix_done_can_enter_takeover_without_reset(self):
        result, bridge, planner, executor, _, _ = self.run_case(
            [{"action": "done"}, {"action": "done"}], [execute_plan()], prefix_decisions=1)
        self.assertEqual(result["takeover_status"], "VALID")
        self.assertEqual(len(executor.calls), 2)
        self.assertEqual(planner.calls[0].history[0].action["action"], "done")
        self.assertEqual(planner.calls[0].decisions_used, 1)
        self.assertEqual(bridge.report()["budget"]["used"], 2)
        self.assertEqual(bridge.coordinator.terminal_reason, "task_complete")

    def test_last_step_verification_done_never_scores_original_success(self):
        result, bridge, planner, executor, device, _ = self.run_case(
            [{"action": "wait"}] * 19 + [{"action": "done"}],
            [verify_plan()], prefix_decisions=18)
        self.assertEqual(result["termination_reason"], "budget_exhausted")
        self.assertIs(result["task_success"], False)
        self.assertIs(result["recovery_success"], False)
        self.assertFalse(result["agent_done"])
        self.assertEqual(len(executor.calls), 20)
        self.assertEqual(len(planner.calls), 1)
        self.assertEqual(len(device.public_actions), 19)
        self.assertEqual(bridge.coordinator.terminal_reason, "budget_exhausted")

    def test_original_completion_on_twentieth_decision_is_scored(self):
        result, _, planner, executor, _, _ = self.run_case(
            [{"action": "wait"}] * 19 + [{"action": "done"}], mode="no_takeover")
        self.assertEqual(result["termination_reason"], "agent_done")
        self.assertIs(result["task_success"], True)
        self.assertEqual(len(executor.calls), 20)
        self.assertFalse(planner.calls)

    def test_cloud_failure_falls_back_without_extra_decisions_or_retry(self):
        result, bridge, planner, executor, _, _ = self.run_case(
            [{"action": "done"}], [RuntimeError("synthetic cloud failure")])
        self.assertEqual(result["termination_reason"], "agent_done")
        self.assertEqual(len(planner.calls), 1)
        self.assertEqual(executor.calls[0][1].scope, "fallback")
        self.assertEqual(bridge.report()["cloud_calls_after_handoff"], 0)

    def test_device_failure_acknowledgement_terminates_coordinator(self):
        result, bridge, _, executor, device, _ = self.run_case(
            [{"action": "wait"}], mode="no_takeover", fail_action=True)
        self.assertEqual(result["termination_reason"], "device_error")
        self.assertEqual(bridge.coordinator.terminal_reason, "device_error")
        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(len(device.public_actions), 1)

    def test_native_executor_answer_has_verification_scope_and_keeps_native_history(self):
        def response(arguments):
            raw = 'Action: Inspect visible state\n<tool_call>\n' + json.dumps(
                {"name": "mobile_use", "arguments": arguments}) + '\n</tool_call>'
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=raw, tool_calls=None))])
        create = Mock(side_effect=[response({"action": "answer", "text": "Visible evidence read"}),
                                  response({"action": "terminate", "status": "success"})])
        client = Mock()
        client.chat.completions.create = create
        factory = Mock()
        factory.return_value.__enter__ = Mock(return_value=client)
        factory.return_value.__exit__ = Mock(return_value=False)
        executor = GUIOwlExecutor(self.root, GUIOwlConfiguration(), client_factory=factory)
        result, bridge, planner, _, device, _ = self.run_case(
            [], [verify_plan(), execute_plan()], executor=executor)
        self.assertEqual(result["termination_reason"], "agent_done")
        self.assertEqual(result["counts"]["decisions"]["resume"], 2)
        self.assertEqual(create.call_count, 2)
        self.assertEqual(len(executor._context), 1)
        self.assertEqual(len(planner.calls), 2)
        self.assertFalse(device.public_actions)
        first, second = [call.kwargs["messages"] for call in create.call_args_list]
        self.assertIn('"scope": "verification"', first[-1]["content"][-1]["text"])
        self.assertIn('"scope": "execution"', second[-1]["content"][-1]["text"])
        self.assertIn("Visible evidence read", str(second))
        self.assertEqual(bridge.report()["cloud_calls_after_handoff"], 0)

    def test_callbacks_are_notification_only_and_mismatched_budget_is_rejected(self):
        planner, executor = Planner([execute_plan()]), Executor([{"action": "done"}])
        bridge = HandoffRecoveryAgent(bench_path=self.root, planner=planner, executor=executor)
        from resumebench.core.agent_protocol import ControlEvent, DecisionContext, TaskStart
        bridge.on_task_start(TaskStart(1, "Public goal", 19))
        with self.assertRaises(ValueError):
            bridge.on_control_event(ControlEvent("suspended", None, 0, 20))
        bridge.on_control_event(ControlEvent("suspended", None, 0, 19))
        observation = Device().capture(Path(self.directory.name) / "fresh")
        bridge.on_control_event(ControlEvent("returned", observation, 0, 19))
        with self.assertRaises(ValueError):
            bridge.on_decision_start(DecisionContext(1, 0, 5, 20))
        bridge.on_decision_start(DecisionContext(1, 0, 19, 19))
        self.assertFalse(planner.calls)
        self.assertFalse(executor.calls)
        self.assertEqual(bridge.decide(observation, "Public goal", ["IGNORED"]), {"action": "done"})
        self.assertEqual(len(planner.calls), 1)
        self.assertEqual(len(executor.calls), 1)


if __name__ == "__main__":
    unittest.main()
