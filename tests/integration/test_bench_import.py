"""Opt-in, offline public-helper parity tests against a real Benchmark checkout.

Set HANDOFFBENCH_ROOT explicitly. SDK calls are mocked; no device or model
service is used, and Benchmark source, credentials and result files are untouched.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from importlib import import_module
import json
import os
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zlib

from handoff_recovery.core.types import Handoff, LocalInstruction, PublicFrame, VerificationTask
from handoff_recovery.core.protocols import recovery_task_goal
from handoff_recovery.executor import ExecutorError, GUIOwlConfiguration, GUIOwlExecutor
from handoff_recovery.integrations.handoffbench import load_gui_owl_bindings, load_phonebuddy_bindings


def png_bytes(color):
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 56, 112, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\x00" + bytes([color, 0, 0]) * 56) * 112))
            + chunk(b"IEND", b""))


def native(arguments):
    return 'Action: Inspect the visible clock\n<tool_call>\n' + json.dumps({"name": "mobile_use", "arguments": arguments}) + '\n</tool_call>'


@unittest.skipUnless(os.environ.get("HANDOFFBENCH_ROOT"), "Set HANDOFFBENCH_ROOT for offline real-helper integration")
class BenchmarkPublicHelpersTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(os.environ["HANDOFFBENCH_ROOT"]).resolve()
        cls.bindings = load_gui_owl_bindings(cls.root)

    def setUp(self):
        self.message = SimpleNamespace(content=native({"action": "click", "coordinate": [1000, 1000]}), tool_calls=None)
        self.create = Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=self.message)]))
        client = Mock()
        client.chat.completions.create = self.create
        self.factory = Mock()
        self.factory.return_value.__enter__ = Mock(return_value=client)
        self.factory.return_value.__exit__ = Mock(return_value=False)
        self.executor = GUIOwlExecutor(self.root, GUIOwlConfiguration(), client_factory=self.factory)
        self.goal = "Set the existing alarm to 09:00 and preserve its label."
        self.executor.start_task("real-helper-test", self.goal)

    def frame(self, index):
        return PublicFrame(f"visible_{index}", png_bytes(index), 56, 112)

    def test_all_benchmark_imports_belong_to_explicit_root(self):
        for name, module in tuple(sys.modules.items()):
            if name == "resumebench" or name.startswith("resumebench."):
                filename = getattr(module, "__file__", None)
                paths = [filename] if filename else list(module.__path__)
                self.assertTrue(paths, name)
                for path in paths:
                    self.assertTrue(Path(path).resolve().is_relative_to(self.root / "resumebench"), name)

    def test_schema_namespace_created_by_runner_can_precede_executor_import(self):
        import_module("resumebench.core.runner")
        schemas = import_module("resumebench.schemas")
        self.assertIsNone(schemas.__file__)
        self.assertTrue(schemas.__path__)
        self.assertIs(load_gui_owl_bindings(self.root).api, self.bindings.api)

    def test_real_history_native_coordinates_and_latest_only_recovery_injection(self):
        api = self.bindings.api
        scopes = ["execution"] * 6 + ["verification"] * 2 + ["execution"] * 6
        expected_history = []
        previous_scope = None
        with patch.object(api, "validate_configuration", side_effect=AssertionError("Benchmark .env must not be read")):
            for index, scope in enumerate(scopes):
                local = (LocalInstruction(self.goal, (), "verification", verification=VerificationTask(
                    "Read only the currently displayed time", ("Visible alarm time",), ("Do not edit",)))
                         if scope == "verification" else LocalInstruction(self.goal, (), "execution", handoff=Handoff(
                             "Correct the time only if necessary", ("Alarm label",), ("09:00 visible",))))
                if scope != previous_scope:
                    expected_history = []
                self.message.content = native({"action": "click", "coordinate": [1000, 1000]}).replace(
                    "Inspect the visible clock", f"Inspect the visible clock {index}")
                result = self.executor.decide(self.frame(index), local)
                self.assertEqual(result["coords"], [55, 111])
                image = self.executor._context[-1]["image"]
                context = ({"scope": scope, "verification": {
                    "objective": local.verification.objective,
                    "required_evidence": list(local.verification.required_evidence),
                    "final_confirmation": False,
                }} if scope == "verification" else {"scope": scope, "handoff": {
                    "remaining_objective": local.handoff.remaining_objective,
                    "completion_conditions": list(local.handoff.completion_conditions),
                }})
                layered_goal = recovery_task_goal(self.goal, context)
                native_messages = api.build_messages(image, layered_goal, expected_history)
                messages = self.create.call_args.kwargs["messages"]
                self.assertEqual(messages, native_messages)
                self.assertEqual(sum(block["type"] == "image_url" for message in messages
                                     for block in message["content"]), min(len(expected_history), 4) + 1)
                self.assertEqual(messages[0]["content"][0]["text"], api.system_prompt())
                task_text = messages[1]["content"][0]["text"]
                self.assertEqual(task_text.count(self.goal), 1)
                self.assertIn("- [pending] " + ("Visible alarm time" if scope == "verification"
                                                else "09:00 visible"), task_text)
                self.assertNotIn("Last action feedback", task_text)
                self.assertFalse(Path(image.path).exists())
                expected_history.append({"output": self.message.content, "image": image})
                for item in expected_history[:-4]:
                    item["image"] = None
                self.assertEqual(self.executor._context, expected_history)
                previous_scope = scope
        self.assertEqual(self.create.call_count, len(scopes))
        self.assertEqual(len(self.executor._context), 6)
        self.assertIn("Previous actions:\nStep 1:", self.create.call_args.kwargs["messages"][1]["content"][0]["text"])

    def test_native_completion_scope_and_strict_parser(self):
        self.executor.decide(self.frame(0), LocalInstruction(self.goal, (), "original"))
        original_history = deepcopy(self.executor._context)
        for scope in ("verification", "execution"):
            local = (LocalInstruction(self.goal, (), scope, verification=VerificationTask(
                "Inspect time", ("Time shown",), ())) if scope == "verification" else
                LocalInstruction(self.goal, (), scope, handoff=Handoff("Finish original goal", (), ("09:00 shown",))))
            for args, expected in (({"action": "answer", "text": "Observed time"}, "done"),
                                   ({"action": "terminate", "status": "success"}, "done"),
                                   ({"action": "terminate", "status": "failure"}, "fail")):
                self.message.content = native(args)
                self.assertEqual(self.executor.decide(self.frame(1), local)["action"], expected)
        before = deepcopy(self.executor._context)
        for raw, category in (("not native", "native_parse_error"),
                              (native({"action": "click", "coordinate": [1001, 500]}), "action_mapping_error"),
                              (native({"action": "interact", "text": "Ask user"}), "action_mapping_error")):
            self.message.content = raw
            with self.assertRaises(ExecutorError) as caught:
                self.executor.decide(self.frame(2), local)
            self.assertEqual(caught.exception.category, category)
            self.assertTrue(caught.exception.recoverable)
            self.assertEqual(self.executor._context, before)
        self.message.content = "not native"
        with self.assertRaises(ExecutorError):
            self.executor.decide(self.frame(3), LocalInstruction(self.goal, (), "original"))
        self.assertEqual(self.executor._context, original_history)
        self.assertEqual(self.executor._contexts["execution"], before)
        self.assertEqual(self.create.call_count, 11)

    def test_phonebuddy_parser_accepts_simulated_completion_check(self):
        bindings = load_phonebuddy_bindings(self.root)
        raw = ('<think>Both visible conditions were checked.</think>'
               '<tool_call>{"name":"finish","arguments":'
               '{"text":"done","completion_check":['
               '{"condition_id":"c1","status":"satisfied",'
               '"evidence":"Saved target is visible."},'
               '{"condition_id":"c2","status":"satisfied",'
               '"evidence":"Unrelated item is unchanged."}]}}</tool_call>')
        call, thought = bindings.parse_tool_call(raw)
        self.assertEqual(call["name"], "finish")
        self.assertEqual(thought, "Both visible conditions were checked.")
        self.assertEqual([item["condition_id"] for item in call["arguments"]["completion_check"]],
                         ["c1", "c2"])
        mapping_call = dict(call, arguments=dict(call["arguments"]))
        mapping_call["arguments"].pop("completion_check")
        mapped = bindings.map_phonebuddy_action(mapping_call, (56, 112))
        self.assertEqual(mapped["action"], "done")


if __name__ == "__main__":
    unittest.main()
