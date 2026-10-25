"""Native baseline/Recovery parity with real adapters and mocked model I/O."""
from copy import deepcopy
import contextlib
from importlib import import_module
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from handoff_recovery.executor import ExecutorSpec, create_executor
from handoff_recovery.integrations.handoffbench import HandoffRecoveryAgent
from tests.integration import test_native_alignment as native_alignment
from tests.integration.test_runner_bridge import (
    Checker, Device, Planner, complete_plan, execute_plan, verify_plan,
)


@unittest.skipUnless(os.environ.get("HANDOFFBENCH_ROOT"), "Set HANDOFFBENCH_ROOT for native parity")
class NativeExecutionParityTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(os.environ["HANDOFFBENCH_ROOT"]).resolve()

    def cases(self):
        clicks = [
            'Action: Tap.\n<tool_call>{"name":"mobile_use","arguments":{"action":"click","coordinate":[500,500]}}</tool_call>',
            'Thought: Tap.\n<tool_call>{"name":"click","arguments":{"points":[[500,500]]}}</tool_call>',
            'Thought: Tap.\nAction: Tap.\n<tool_call>{"name":"mobile_use","arguments":{"action":"click","coordinate":[500,500]}}</tool_call>',
            '<tool_call><function=click><parameter=element>Save</parameter><parameter=x>500</parameter><parameter=y>500</parameter></function></tool_call>',
        ]
        for case, click in zip(native_alignment.NativeAlignmentTests.cases(self), clicks):
            cls, configuration, load, finish = case
            yield cls, configuration, load, finish, click

    def baseline(self, name, configuration):
        module = import_module("resumebench.agents." + name)
        cls = {"gui_owl": "GUIOwlAgent", "phonebuddy": "PhoneBuddyAgent",
               "ui_voyager": "UIVoyagerAgent", "holo": "HoloAgent"}[name]
        agent = getattr(module, cls)()
        agent._configuration = configuration
        return agent

    def run_agent(self, agent, directory, *, mode="no_takeover", budget=4, prefix_decisions=0,
                  goal_status="false"):
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
        device = Device()
        kwargs = {"scenario": scenario, "flow": flow} if mode == "takeover" else {}
        result = run_task(task=task, mode=mode, device=device, agent=agent,
                          checker=Checker(goal_status=goal_status, prefix_decisions=prefix_decisions), run_dir=directory,
                          max_decisions=budget, settle_seconds=0, **kwargs)
        return result, device

    def test_invalid_native_outputs_consume_budget_and_continue_like_baseline(self):
        for cls, configuration, load, finish, click in self.cases():
            bindings = load(self.root)
            executor = cls(self.root, configuration)
            name = executor.descriptor["adapter"]
            bad_mapping = click.replace("500", "1001", 1)
            for mode in ("no_takeover", "takeover"):
                with self.subTest(adapter=name, mode=mode), tempfile.TemporaryDirectory() as directory:
                    results = []
                    for recovery in (False, True):
                        planner = Planner([execute_plan()])
                        executor = cls(self.root, configuration)
                        agent = (HandoffRecoveryAgent(bench_path=self.root, planner=planner,
                                 executor=executor, completion_policy="native") if recovery else
                                 self.baseline(name, configuration))
                        with patch.object(bindings.api, "request_action", side_effect=[
                                "<tool_call>malformed</tool_call>", bad_mapping, click, finish]) as request:
                            result, device = self.run_agent(agent, Path(directory)/str(recovery), mode=mode)
                        self.assertEqual(request.call_count, 4)
                        self.assertEqual(result["termination_reason"], "agent_done")
                        self.assertEqual(sum(result["counts"]["decisions"].values()), 4)
                        self.assertEqual(len(device.public_actions), 1)
                        self.assertIs(result["task_success"], False)
                        results.append(device.public_actions)
                        if recovery:
                            errors = [item.action.get("error_category") for item in agent.coordinator._history
                                      if item.action["action"] == "invalid_decision"]
                            self.assertEqual(errors, ["native_parse_error", "action_mapping_error"])
                            self.assertEqual(len(executor._context), 2)
                            self.assertEqual(len(planner.calls), int(mode == "takeover"))
                    self.assertEqual(results[0], results[1])

    def test_invalid_outputs_exhaust_shared_budget_without_device_actions(self):
        for cls, configuration, load, _, _ in self.cases():
            bindings = load(self.root)
            with self.subTest(adapter=cls.__name__), tempfile.TemporaryDirectory() as directory:
                bridge = HandoffRecoveryAgent(bench_path=self.root, planner=Planner([execute_plan()]),
                    executor=cls(self.root, configuration), completion_policy="native")
                with patch.object(bindings.api, "request_action", return_value="<tool_call>invalid</tool_call>") as request:
                    result, device = self.run_agent(bridge, Path(directory)/"run", mode="takeover", budget=2)
                self.assertEqual(result["termination_reason"], "budget_exhausted")
                self.assertEqual(request.call_count, 2)
                self.assertEqual(bridge.coordinator.decisions_used, 2)
                self.assertEqual(device.public_actions, [])

    def test_native_phonebuddy_rejects_completion_check_extension_like_baseline(self):
        cls, configuration, load, finish, _ = list(self.cases())[1]
        bindings = load(self.root)
        extended = finish.replace('"text":"Done"', '"text":"Done","completion_check":[]')
        baseline_history = None
        for recovery in (False, True):
            with self.subTest(recovery=recovery), tempfile.TemporaryDirectory() as directory:
                executor = cls(self.root, configuration)
                agent = (HandoffRecoveryAgent(bench_path=self.root, planner=Planner(), executor=executor,
                         completion_policy="native") if recovery else self.baseline("phonebuddy", configuration))
                with patch.object(bindings.api, "request_action", side_effect=[extended, finish]) as request:
                    result, device = self.run_agent(agent, Path(directory)/"run", budget=2)
                self.assertEqual(result["termination_reason"], "agent_done")
                self.assertEqual(request.call_count, 2)
                self.assertEqual(device.public_actions, [])
                if recovery:
                    self.assertEqual(len(executor._context), 1)
                    self.assertEqual(executor._context, baseline_history)
                else:
                    baseline_history = deepcopy(agent._context)

    def test_ui_voyager_and_holo_resolve_the_baseline_configuration(self):
        for cls, configuration, load, _, _ in list(self.cases())[2:]:
            bindings = load(self.root)
            name = cls(self.root, configuration).descriptor["adapter"]
            selected = SimpleNamespace(api_key="synthetic-local-key", model="selected-model",
                base_url="http://selected.invalid/v1", timeout_seconds=231.0, max_tokens=731)
            with self.subTest(adapter=name), patch.object(bindings.api, "validate_configuration", return_value=selected) as validate:
                executor = create_executor(name, self.root, ExecutorSpec(agent=name, model="ignored",
                    base_url="http://ignored.invalid/v1", max_tokens=4096,
                    configuration_source="handoffbench_provider_validate_configuration"))
                validate.assert_called_once_with()
                for field in ("api_key", "model", "base_url", "timeout_seconds", "max_tokens"):
                    self.assertEqual(getattr(executor._configuration, field), getattr(selected, field))
                self.assertEqual(executor.descriptor["configuration_source"],
                                 "handoffbench_provider_validate_configuration")
                self.assertNotIn(selected.api_key, repr(executor.descriptor))

    def test_original_requests_and_native_history_match_baseline(self):
        from handoff_recovery.core.types import LocalInstruction, PublicFrame
        for cls, configuration, load, finish, click in self.cases():
            bindings = load(self.root)
            executor = cls(self.root, configuration)
            name = executor.descriptor["adapter"]
            with self.subTest(adapter=name), tempfile.TemporaryDirectory() as directory:
                calls = []
                for recovery in (False, True):
                    replies = iter((click, finish))
                    create = Mock(side_effect=lambda **kwargs: SimpleNamespace(choices=[SimpleNamespace(
                        message=SimpleNamespace(content=next(replies), tool_calls=[], reasoning_content=""))]))
                    client = Mock()
                    client.chat.completions.create = create
                    factory = Mock()
                    factory.return_value.__enter__ = Mock(return_value=client)
                    factory.return_value.__exit__ = Mock(return_value=False)
                    executor = cls(self.root, configuration)
                    agent = self.baseline(name, configuration)
                    executor.start_task("synthetic", "Set test option")
                    history = []
                    with patch.object(bindings.api, "_openai_class", return_value=factory), \
                            patch.object(bindings.api, "system_prompt", return_value="Synthetic native prompt."):
                        for index in range(2):
                            observation = Device().capture(Path(directory)/str(recovery)/str(index))
                            frame = PublicFrame("screen_"+str(index), Path(observation["screenshot"]).read_bytes(), 56, 112)
                            action = (executor.decide(frame, LocalInstruction("Set test option", (), "original",
                                      completion_policy="native")) if recovery else
                                      agent.decide(observation, "Set test option", history))
                            history.append(action)
                    self.assertEqual(create.call_count, 2)
                    for call in factory.call_args_list:
                        self.assertEqual(call.kwargs["max_retries"], 0)
                    calls.append([deepcopy(call.kwargs) for call in create.call_args_list])
                self.assertEqual(calls[0], calls[1])

    def test_native_verification_and_final_confirmation_keep_completion_scoped(self):
        for cls, configuration, load, finish, click in self.cases():
            bindings = load(self.root)
            for final in (False, True):
                with self.subTest(adapter=cls.__name__, final=final), tempfile.TemporaryDirectory() as directory:
                    planner = Planner([complete_plan()] if final else [verify_plan(), execute_plan()])
                    executor = cls(self.root, configuration)
                    bridge = HandoffRecoveryAgent(bench_path=self.root, planner=planner,
                        executor=executor, completion_policy="native")
                    replies = [click, finish] if final else [click, finish, finish]
                    with patch.object(bindings.api, "request_action", side_effect=replies) as request:
                        result, device = self.run_agent(bridge, Path(directory)/"run", mode="takeover")
                    self.assertEqual(result["termination_reason"], "agent_done")
                    self.assertIs(result["task_success"], False)
                    self.assertEqual(len(device.public_actions), 1)
                    self.assertEqual(len(planner.calls), 1 if final else 2)
                    self.assertEqual(request.call_count, len(replies))
                    self.assertEqual([len(call.kwargs["history"]) for call in request.call_args_list],
                                     [0, 1] if final else [0, 1, 0])
                    self.assertEqual(result["public_agent_protocol"]["subtask_completions"], 0 if final else 1)
                    self.assertEqual(bridge.report()["cloud_calls_after_handoff"], 0)
                    for call in request.call_args_list:
                        self.assertIn("Set test option", call.args[1])
                        self.assertNotIn('"condition_updates":', call.args[1])

    def test_planner_failure_restores_original_native_goal_and_history(self):
        for cls, configuration, load, finish, click in self.cases():
            bindings = load(self.root)
            with self.subTest(adapter=cls.__name__), tempfile.TemporaryDirectory() as directory:
                planner = Planner([RuntimeError("Synthetic Planner failure")])
                executor = cls(self.root, configuration)
                bridge = HandoffRecoveryAgent(bench_path=self.root, planner=planner,
                    executor=executor, completion_policy="native")
                with patch.object(bindings.api, "request_action", side_effect=[click, finish]) as request:
                    result, _ = self.run_agent(bridge, Path(directory)/"run", mode="takeover", prefix_decisions=1)
                self.assertEqual(result["termination_reason"], "agent_done")
                self.assertEqual(result["counts"]["decisions"], {"task": 0, "prefix": 1, "resume": 1})
                self.assertEqual([call.args[1] for call in request.call_args_list], ["Set test option"]*2)
                self.assertEqual([len(call.kwargs["history"]) for call in request.call_args_list], [0, 1])
                self.assertEqual(executor._context_scope, "original")
                self.assertEqual(len(planner.calls), 1)

    def test_last_budgeted_native_verification_completion_never_ends_original_task(self):
        for cls, configuration, load, finish, click in self.cases():
            bindings = load(self.root)
            with self.subTest(adapter=cls.__name__), tempfile.TemporaryDirectory() as directory:
                planner = Planner([verify_plan()])
                executor = cls(self.root, configuration)
                bridge = HandoffRecoveryAgent(bench_path=self.root, planner=planner,
                    executor=executor, completion_policy="native")
                with patch.object(bindings.api, "request_action", side_effect=[click, finish]) as request:
                    result, device = self.run_agent(bridge, Path(directory)/"run", mode="takeover",
                        budget=2, goal_status="true")
                self.assertEqual(result["termination_reason"], "budget_exhausted")
                self.assertIs(result["task_success"], False)
                self.assertFalse(result["agent_done"])
                self.assertEqual(result["public_agent_protocol"]["subtask_completions"], 1)
                self.assertEqual(len(device.public_actions), 1)
                self.assertEqual(request.call_count, 2)
                self.assertEqual(len(planner.calls), 1)
                self.assertEqual(bridge.coordinator.terminal_reason, "budget_exhausted")

    def test_scripts_record_effective_native_configuration_before_device_access(self):
        scripts = Path(__file__).resolve().parents[2]/"scripts"
        selected = SimpleNamespace(api_key="synthetic-local-key", model="selected-model",
            base_url="http://selected.invalid/v1", timeout_seconds=231.0, max_tokens=731,
            prompt_path=None)
        for name in ("gui_owl", "phonebuddy", "ui_voyager", "holo"):
            for script in ("run_live_smoke", "run_recovery_batch"):
                with self.subTest(adapter=name, script=script), tempfile.TemporaryDirectory() as directory:
                    spec = importlib.util.spec_from_file_location("native_config_"+script, scripts/(script+".py"))
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    argv = ["--bench-root", str(self.root), "--local-agent", name]
                    with patch("handoff_recovery.executor.registry.load_local_configuration", return_value=selected), \
                            patch.object(sys, "path", [str(self.root), *sys.path]), \
                            contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        if script == "run_live_smoke":
                            output = Path(directory)/"smoke"
                            argv += ["--baseline", "/unused.json", "--device", "synthetic", "--adb", "/adb",
                                     "--appium-url", "http://appium.invalid", "--mode", "no_takeover", "--instances", "3",
                                     "--local-model", "stale-model", "--local-base-url", "http://stale.invalid/v1",
                                     "--configuration-source", "handoffbench_provider_validate_configuration",
                                     "--output", str(output)]
                            import_module("scripts.run_resumebench_batch")
                            with patch("resumebench.environments.baseline.load_baseline", return_value={"baseline_id":"synthetic"}), \
                                    patch("scripts.run_resumebench_batch.device_lock", side_effect=RuntimeError("test-stop-before-device")):
                                with self.assertRaisesRegex(RuntimeError, "test-stop-before-device"):
                                    module.main(argv)
                            summary = json.loads((output/"summary.json").read_text())
                        else:
                            values = {"RECOVERY_PLANNER_PROVIDER": "synthetic",
                                      "RECOVERY_PLANNER_SYNTHETIC_ENDPOINT": "https://planner.invalid/v1/chat/completions",
                                      "RECOVERY_PLANNER_SYNTHETIC_MODEL": "synthetic",
                                      "RECOVERY_PLANNER_SYNTHETIC_API_KEY": "synthetic"}
                            captured = []
                            def capture_summary(path):
                                captured.append(json.loads((path/"summary.json").read_text()))
                            with patch.object(module, "_read_env", return_value=values), \
                                    patch.object(module, "_select_tasks", return_value=[(3, 2002)]), \
                                    patch.object(module, "_task_metadata", return_value={}), \
                                    patch.object(module.shutil, "rmtree", side_effect=capture_summary), \
                                    patch.object(module, "_run_child", side_effect=AssertionError("No child in dry run")):
                                try:
                                    self.assertEqual(module.main(argv+["--all", "--dry-run", "--output-root", directory]), 0)
                                finally:
                                    for stream in getattr(sys.stdout, "streams", ())[1:]:
                                        stream.close()
                            summary = captured[0]
                    effective = summary["executor"]
                    self.assertEqual(effective["model"], selected.model)
                    self.assertEqual(effective["base_url"], selected.base_url)
                    self.assertEqual(effective["timeout_seconds"], selected.timeout_seconds)
                    self.assertEqual(effective["max_output_tokens"], selected.max_tokens)
                    self.assertEqual(effective["configuration_source"], "handoffbench_provider_validate_configuration")
                    self.assertNotIn(selected.api_key, json.dumps(summary))
