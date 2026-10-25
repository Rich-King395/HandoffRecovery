"""Optional claims around real native parsers with mocked model transport."""
import json
import contextlib
import io
import os
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from handoff_recovery.core.coordinator import RecoveryCoordinator
from handoff_recovery.core.types import LocalInstruction, Handoff, RecoveryBudget
from handoff_recovery.executor.condition_updates import FORMAT_EXTENSION
from handoff_recovery.integrations.handoffbench import HandoffRecoveryAgent
from tests.integration import test_native_execution_parity as parity_cases
from tests.integration.test_runner_bridge import Planner, execute_plan
from tests.unit.test_coordinator import frame


@unittest.skipUnless(os.environ.get("HANDOFFBENCH_ROOT"), "Set HANDOFFBENCH_ROOT for native claims")
class NativeConditionUpdatesTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(os.environ["HANDOFFBENCH_ROOT"])

    def cases(self):
        helper = parity_cases.NativeExecutionParityTests()
        helper.root = self.root
        return helper.cases()

    def response(self, bindings, raw, metadata):
        text = raw + "\n<condition_updates>" + json.dumps(metadata) + "</condition_updates>"
        return bindings.api.Completion(text) if hasattr(bindings.api, "Completion") else text

    def test_all_native_actions_and_terminal_claims_keep_history_and_budget(self):
        claims = [{"condition_id": "c1", "status": "satisfied", "evidence": "Saved value is visible"}]
        for cls, config, load, finish, click in self.cases():
            for policy in ("native", "conditions"):
                with self.subTest(adapter=cls.__name__, policy=policy):
                    bindings = load(self.root)
                    executor = cls(self.root, config)
                    executor._bindings = bindings
                    planner = Planner([execute_plan()])
                    control = RecoveryCoordinator(task_id="synthetic", original_goal="Save the requested value",
                        planner=planner, executor=executor, budget=RecoveryBudget(total_decisions=2),
                        completion_policy=policy, condition_updates_enabled=True)
                    control.pause_for_handoff()
                    control.return_control(frame("returned"))
                    with patch.object(bindings.api, "request_action", side_effect=[
                            self.response(bindings, click, []), self.response(bindings, finish, claims)]) as request:
                        self.assertEqual(control.step(frame("first")).kind, "action")
                        control.acknowledge_action(success=True)
                        self.assertEqual(control.step(frame("second")).kind, "task_complete")
                    self.assertEqual((control.decisions_used, request.call_count), (2, 2))
                    self.assertEqual(len(planner.calls), 1)
                    self.assertEqual(control.report()["condition_status"][0]["status"], "satisfied")
                    self.assertNotIn("condition_updates", str(executor._context))
                    self.assertEqual(request.call_args.kwargs["response_format_extension"], FORMAT_EXTENSION)

    def test_native_sdk_wire_adds_only_the_opt_in_format_extension(self):
        claims = [{"condition_id": "c1", "status": "satisfied", "evidence": "Visible saved value"}]
        for cls, config, load, finish, _click in self.cases():
            bindings = load(self.root)
            with self.subTest(adapter=cls.__name__), tempfile.TemporaryDirectory() as directory:
                if cls.__name__ == "PhoneBuddyExecutor":
                    prompt = Path(directory) / "native.txt"
                    prompt.write_text("Synthetic native tools. Keep the native call format.")
                    config = replace(config, prompt_path=prompt)
                previous_parameters = None
                for active in (False, True):
                    executor = cls(self.root, config)
                    executor._bindings = bindings
                    executor.start_task("synthetic", "Save value")
                    text = (finish + "\n<condition_updates>" + json.dumps(claims) + "</condition_updates>"
                            if active else finish)
                    message = SimpleNamespace(content=text, tool_calls=[], reasoning_content=None, reasoning=None)
                    create = Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=message)]))
                    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
                    class Client:
                        def __init__(self, **_kwargs):
                            pass
                        def __enter__(self):
                            return client
                        def __exit__(self, *_args):
                            pass
                    instruction = LocalInstruction("Save value", (), "execution",
                        handoff=Handoff("Finish saving", (), ("Saved value is visible",)),
                        completion_policy="native", condition_updates_enabled=active)
                    prompt_reader = (patch.object(bindings.api, "system_prompt", return_value=prompt.read_text())
                                     if cls.__name__ == "PhoneBuddyExecutor" else contextlib.nullcontext())
                    with patch.object(bindings.api, "_openai_class", return_value=Client), \
                            prompt_reader, \
                            contextlib.redirect_stdout(io.StringIO()):
                        action = executor.decide(frame("current"), instruction)
                    self.assertEqual(create.call_count, 1)
                    parameters = dict(create.call_args.kwargs)
                    messages = parameters.pop("messages")
                    texts = [m["content"] if isinstance(m["content"], str) else
                             "\n".join(x.get("text", "") for x in m["content"]) for m in messages]
                    self.assertEqual("Recovery condition metadata v1" in "\n".join(texts), active)
                    self.assertEqual(action["action"], "done")
                    if active:
                        self.assertEqual(action["condition_updates"], claims)
                        self.assertEqual(parameters, previous_parameters)
                    previous_parameters = parameters
                    self.assertNotIn("condition_updates", str(executor._context))

    def test_holo_structured_tool_call_can_carry_content_metadata(self):
        for cls, config, load, finish, click in self.cases():
            if cls.__name__ != "HoloExecutor":
                continue
            bindings = load(self.root)
            response = bindings.api.Completion(
                '<condition_updates>[{"condition_id":"c1","status":"satisfied","evidence":"Visible"}]</condition_updates>',
                [{"id": "synthetic", "type": "function",
                  "function": {"name": "answer", "arguments": {"content": "Done"}}}])
            executor = cls(self.root, config)
            executor._bindings = bindings
            executor.start_task("synthetic", "Save value")
            instruction = LocalInstruction("Save value", (), "execution",
                handoff=Handoff("Finish", (), ("Saved value is visible",)),
                completion_policy="native", condition_updates_enabled=True)
            with patch.object(bindings.api, "request_action", return_value=response):
                action = executor.decide(frame("current"), instruction)
            self.assertEqual(action["action"], "done")
            self.assertEqual(action["condition_updates"][0]["condition_id"], "c1")
            self.assertEqual(executor._context[0]["assistant"]["content"], "")

    def test_disabled_uses_strict_native_parser_and_no_format_extension(self):
        for cls, config, load, finish, click in self.cases():
            with self.subTest(adapter=cls.__name__):
                bindings = load(self.root)
                executor = cls(self.root, config)
                executor._bindings = bindings
                executor.start_task("synthetic", "Save value")
                instruction = LocalInstruction("Save value", (), "execution",
                    handoff=Handoff("Finish", (), ("Saved value is visible",)),
                    completion_policy="native", condition_updates_enabled=False)
                raw = bindings.api.Completion(finish) if hasattr(bindings.api, "Completion") else finish
                with patch.object(bindings.api, "request_action", return_value=raw) as request:
                    self.assertEqual(executor.decide(frame("current"), instruction)["action"], "done")
                self.assertNotIn("response_format_extension", request.call_args.kwargs)
                self.assertNotIn("condition_updates", str(executor._context))

    def test_runner_bridge_accepts_terminal_updates_on_last_budgeted_decision(self):
        helper = parity_cases.NativeExecutionParityTests()
        helper.root = self.root
        claims = [{"condition_id": "c1", "status": "satisfied", "evidence": "Saved value visible"}]
        for cls, config, load, finish, _click in self.cases():
            bindings = load(self.root)
            for policy in ("native", "conditions"):
                with self.subTest(adapter=cls.__name__, policy=policy), tempfile.TemporaryDirectory() as directory:
                    executor = cls(self.root, config)
                    executor._bindings = bindings
                    bridge = HandoffRecoveryAgent(bench_path=self.root,
                        planner=Planner([execute_plan()]), executor=executor,
                        action_validator=bindings.validate_action, completion_policy=policy,
                        condition_updates_enabled=True)
                    with patch.object(bindings.api, "request_action", return_value=self.response(bindings, finish, claims)) as request:
                        result, _device = helper.run_agent(bridge, Path(directory) / "run", mode="takeover",
                            prefix_decisions=0, budget=1, goal_status="true")
                    self.assertIs(result["task_success"], True)
                    self.assertEqual(result["termination_reason"], "agent_done")
                    self.assertEqual((bridge.coordinator.decisions_used, request.call_count), (1, 1))
                    self.assertEqual(bridge.report()["condition_status"][0]["status"], "satisfied")
