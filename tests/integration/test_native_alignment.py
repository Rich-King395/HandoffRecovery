"""Real native parsers, bridge, Runner and scoring; model/device I/O is mocked."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from handoff_recovery.core.types import LocalInstruction
from handoff_recovery.executor import (
    GUIOwlConfiguration, GUIOwlExecutor, PhoneBuddyConfiguration, PhoneBuddyExecutor,
    UIVoyagerConfiguration, UIVoyagerExecutor, HoloConfiguration, HoloExecutor,
)
from handoff_recovery.integrations.handoffbench import (
    HandoffRecoveryAgent, load_gui_owl_bindings, load_phonebuddy_bindings,
    load_ui_voyager_bindings, load_holo_bindings,
)
from tests.integration.test_runner_bridge import Device, Checker, Planner, execute_plan, verify_plan
from tests.unit.test_coordinator import frame


@unittest.skipUnless(os.environ.get("HANDOFFBENCH_ROOT"), "Set HANDOFFBENCH_ROOT for native alignment")
class NativeAlignmentTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(os.environ["HANDOFFBENCH_ROOT"]).resolve()

    def cases(self):
        return [
            (GUIOwlExecutor, GUIOwlConfiguration(), load_gui_owl_bindings,
             'Action: Finish.\n<tool_call>{"name":"mobile_use","arguments":{"action":"answer","text":"Done"}}</tool_call>'),
            (PhoneBuddyExecutor, PhoneBuddyConfiguration(), load_phonebuddy_bindings,
             'Thought: Finished.\n<tool_call>{"name":"finish","arguments":{"text":"Done"}}</tool_call>'),
            (UIVoyagerExecutor, UIVoyagerConfiguration(), load_ui_voyager_bindings,
             'Thought: Finished.\nAction: Finish.\n<tool_call>{"name":"mobile_use","arguments":{"action":"terminate","status":"success"}}</tool_call>'),
            (HoloExecutor, HoloConfiguration(), load_holo_bindings,
             '<tool_call><function=answer><parameter=content>\nDone\n</parameter></function></tool_call>'),
        ]

    def test_all_local_native_completions_reach_independent_scoring(self):
        for cls, configuration, load, raw in self.cases():
            bindings = load(self.root)
            for goal_status in ("true", "false"):
                with self.subTest(adapter=cls.__name__, score=goal_status), tempfile.TemporaryDirectory() as directory:
                    executor = cls(self.root, configuration)
                    executor._bindings = bindings
                    planner = Planner([execute_plan()])
                    bridge = HandoffRecoveryAgent(bench_path=self.root, planner=planner,
                        executor=executor, action_validator=bindings.validate_action,
                        completion_policy="native")
                    from resumebench.core.runner import run_task
                    task = {"id": 987654, "app": "synthetic.settings", "description": "Set test option",
                            "rule_key": "goal", "environment": {"width": 56, "height": 112}}
                    scenario = {"id": "synthetic_takeover", "task_id": task["id"], "entry_check": "entry",
                                "flow_id": "synthetic_flow", "resume_checks": []}
                    flow = {"timeout_seconds": 2, "before_check": "entry", "after_check": "handoff",
                        "initial_state": "entry", "max_transitions": 1,
                        "locators": {"button": {"by": "accessibility_id", "value": "Synthetic"}},
                        "states": {"entry": {"check": "entry", "action": {"op": "click", "target": "button"},
                                              "next": "returned"},
                                   "returned": {"check": "handoff", "terminal": True}}}
                    with patch.object(bindings.api, "request_action", return_value=raw) as request:
                        result = run_task(task=task, mode="takeover", device=Device(), agent=bridge,
                            checker=Checker(goal_status=goal_status), scenario=scenario, flow=flow,
                            run_dir=Path(directory)/"run", max_decisions=2, settle_seconds=0)
                    self.assertEqual(result["termination_reason"], "agent_done")
                    self.assertEqual(sum(result["counts"]["decisions"].values()), 1)
                    self.assertEqual(result["takeover_status"], "VALID")
                    self.assertEqual(result["task_success"], goal_status == "true")
                    self.assertEqual(bridge.report()["condition_status"][0]["status"], "pending")
                    self.assertEqual(len(planner.calls), 1)
                    request.assert_called_once()
                    self.assertEqual(executor._context_scope, "execution")

    def test_gui_owl_and_phonebuddy_share_strict_baseline_mapping(self):
        for cls, configuration, load, _ in self.cases()[:2]:
            bindings = load(self.root)
            if cls is GUIOwlExecutor:
                raw = 'Action: Tap.\n<tool_call>{"name":"mobile_use","arguments":{"action":"click","coordinate":[540,1200]}}</tool_call>'
                parse = lambda value: bindings.parse_tool_call(value)[0]
                mapper = bindings.map_mobile_use_action
            else:
                raw = 'Thought: Tap.\n<tool_call>{"name":"click","arguments":{"points":[500,900]}}</tool_call>'
                parse = lambda value: bindings.parse_tool_call(value)[0]
                mapper = bindings.map_phonebuddy_action
            call = parse(raw)
            with self.subTest(adapter=cls.__name__), self.assertRaises(ValueError):
                mapper(deepcopy(call), (2, 2))
            executor = cls(self.root, configuration)
            executor._bindings = bindings
            executor.start_task("synthetic", "Set test option")
            with patch.object(bindings.api, "request_action", return_value=raw) as request:
                with self.assertRaises(Exception) as caught:
                    executor.decide(frame("screen"), LocalInstruction("Set test option", (), "original"))
            self.assertEqual(caught.exception.category, "action_mapping_error")
            self.assertEqual(executor._context, [])
            request.assert_called_once()
