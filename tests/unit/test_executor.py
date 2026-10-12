"""Offline executor contract tests; no SDK, Benchmark checkout, or device needed."""
from __future__ import annotations

import base64
from copy import deepcopy
from importlib.machinery import ModuleSpec
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zlib

from handoff_recovery.core.types import ConditionalAction, Handoff, LocalInstruction, PublicFrame, VerificationTask
from handoff_recovery.core.protocols import HISTORY_POLICY_VERSION, apply_history_policy
from handoff_recovery.executor import ExecutorError, GUIOwlConfiguration, GUIOwlExecutor
from handoff_recovery.integrations.handoffbench import BenchmarkImportError, load_gui_owl_bindings
from handoff_recovery.integrations.handoffbench.loader import _check_loaded_modules


GOAL = "Keep the existing title and set the alarm to 09:00."


def png_bytes(color=0):
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\x00" + bytes([color]) * 6) * 2))
            + chunk(b"IEND", b""))


def frame(index=0):
    return PublicFrame(f"screen_{index}", png_bytes(index), 2, 2)


def instruction(scope="original"):
    return LocalInstruction(
        GOAL, ("Keep the title",), scope,
        verification=(VerificationTask("Read existing alarm time", ("Visible time",),
                                       ("Do not edit alarm data",))
                      if scope == "verification" else None),
        handoff=(Handoff("Set only the incorrect time", ("Existing title",),
                         ("09:00 visible",)) if scope == "execution" else None),
    )


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.baselines = []
        self.histories = []
        self.goals = []
        self.paths = []

        def encode(path, expected_size):
            self.paths.append(Path(path))
            self.assertEqual(Path(path).read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
            return SimpleNamespace(url=base64.b64encode(Path(path).read_bytes()).decode(),
                                   original_size=expected_size)

        def build(image, goal, history):
            self.goals.append(goal)
            self.histories.append(deepcopy(history))
            messages = [{"role": "system", "content": [{"type": "text", "text": "native"}]}]
            messages.append({"role": "user", "content": [{"type": "text", "text": goal}]})
            for record in history:
                messages.append({"role": "assistant", "content": record["output"]})
            messages.append({"role": "user", "content": [{"type": "image_url", "image_url": {"url": image.url}}]})
            self.baselines.append(deepcopy(messages))
            return messages

        self.bindings = SimpleNamespace(
            api=SimpleNamespace(encode_screenshot=Mock(side_effect=encode),
                                build_messages=Mock(side_effect=build), HISTORY_IMAGES=4,
                                TEMPERATURE=0.6, TOP_P=0.95, TOP_K=40),
            parse_tool_call=Mock(return_value=({"native": "call"}, "Read the clock")),
            map_mobile_use_action=Mock(side_effect=lambda _call, _size: {"action": "tap", "coords": [1, 1]}),
            validate_action=Mock(return_value={"action": "tap", "wait_seconds": 0}),
        )
        patcher = patch("handoff_recovery.executor.gui_owl.load_gui_owl_bindings", return_value=self.bindings)
        self.loader = patcher.start()
        self.addCleanup(patcher.stop)
        self.create = Mock(return_value=SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content="native output", tool_calls=None))]))
        self.client = Mock()
        self.client.chat.completions.create = self.create
        self.context_manager = Mock()
        self.context_manager.__enter__ = Mock(return_value=self.client)
        self.context_manager.__exit__ = Mock(return_value=False)
        self.factory = Mock(return_value=self.context_manager)
        self.executor = GUIOwlExecutor("/explicit/benchmark", GUIOwlConfiguration(api_key="private-test-key"),
                                       client_factory=self.factory)
        self.executor.start_task("task-1", GOAL)

    def test_history_policy_is_explicit_and_scope_transitions_are_shared(self):
        self.assertEqual(HISTORY_POLICY_VERSION, "recovery_history_v1")
        contexts = {"original": [{"action": "tap"}]}
        history, scope = apply_history_policy(
            contexts, contexts["original"], "original",
            LocalInstruction(GOAL, (), "execution",
                             handoff=Handoff("Finish the remaining task", (), ("Done",))))
        self.assertEqual((history, scope), ([], "execution"))
        history.append({"action": "wait"})
        history, scope = apply_history_policy(
            contexts, history, scope,
            LocalInstruction(GOAL, (), "fallback", resume_original=True,
                             resume_reason="planner_failed"))
        self.assertEqual((history, scope), ([{"action": "tap"}], "original"))

    def test_one_request_uses_native_sampling_and_no_action_normalization_defaults(self):
        action = self.executor.decide(frame(), instruction())
        self.assertEqual(action, {"action": "tap", "coords": [1, 1], "target": "Read the clock"})
        self.factory.assert_called_once_with(api_key="private-test-key", base_url="http://127.0.0.1:8002/v1",
                                             timeout=180.0, max_retries=0)
        self.create.assert_called_once()
        args = self.create.call_args.kwargs
        self.assertEqual({key: value for key, value in args.items() if key != "messages"}, {
            "model": "GUI-Owl-1.5-4B-Instruct", "stream": False, "max_tokens": 2048,
            "temperature": 0.6, "top_p": 0.95, "extra_body": {"top_k": 40},
        })
        self.context_manager.__exit__.assert_called_once()
        self.assertFalse(any(path.exists() for path in self.paths))

    def test_only_latest_message_receives_context_with_scope_isolation(self):
        scopes = ("original", "verification", "execution", "fallback", "execution", "execution", "execution")
        for index, scope in enumerate(scopes):
            self.create.return_value.choices[0].message.content = f"native output {index}"
            self.executor.decide(frame(index), instruction(scope))
            request = self.create.call_args.kwargs["messages"]
            baseline = self.baselines[-1]
            if scope == "original":
                self.assertEqual(request, baseline)
            else:
                self.assertEqual(request[:-1], baseline[:-1])
                self.assertEqual(request[-1]["content"][:-1], baseline[-1]["content"])
                self.assertIn(f'"scope": "{scope}"', request[-1]["content"][-1]["text"])
            expected = 0
            for prior in reversed(scopes[:index]):
                if prior != scopes[index]:
                    break
                expected += 1
            self.assertEqual(len(self.histories[-1]), expected)
        self.assertEqual(self.goals, [GOAL] * len(scopes))
        self.assertEqual([record["output"] for record in self.executor._context],
                         [f"native output {i}" for i in range(4, 7)])
        self.assertEqual(sum(record["image"] is not None for record in self.executor._context), 3)
        self.assertEqual(self.histories[1], [])

    def test_verification_completion_passes_through_and_is_not_original_completion(self):
        self.bindings.map_mobile_use_action.side_effect = lambda *_: {"action": "done", "coords": [], "reason": "Time is 09:00"}
        action = self.executor.decide(frame(), instruction("verification"))
        self.assertEqual(action["action"], "done")
        prompt = self.create.call_args.kwargs["messages"][-1]["content"][-1]["text"]
        self.assertIn("declares verification complete only", prompt)
        self.assertIn("Do not edit alarm data", prompt)
        self.assertEqual(self.create.call_count, 1)
        self.assertEqual(len(self.executor._context), 1)

    def test_execution_prompt_requires_completion_condition_self_check(self):
        self.executor.decide(frame(), instruction("execution"))
        prompt = self.create.call_args.kwargs["messages"][-1]["content"][-1]["text"]
        self.assertIn("check every completion_condition", prompt)
        self.assertIn("pre-handoff history is not evidence", prompt)

    def test_execution_prompt_includes_local_inspection_branches(self):
        handoff = Handoff("Restore the requested setting.", (), ("The setting is correct.",),
                          ("Open Appearance and read the explicit value.",),
                          (ConditionalAction("The value is Light.", "Select Dark."),))
        self.executor.decide(frame(), LocalInstruction(GOAL, (), "execution", handoff=handoff))
        prompt = self.create.call_args.kwargs["messages"][-1]["content"][-1]["text"]
        self.assertIn("inspect_before_action", prompt)
        self.assertIn("Select Dark.", prompt)

    def test_start_task_is_only_reset_even_for_identical_goal(self):
        self.executor.decide(frame(), instruction())
        with self.assertRaisesRegex(ValueError, "original goal cannot change"):
            self.executor.decide(frame(1), LocalInstruction("different task", (), "original"))
        self.assertEqual(len(self.executor._context), 1)
        self.assertEqual(self.create.call_count, 1)
        self.executor.start_task("task-2", GOAL)
        self.executor.decide(frame(2), instruction())
        self.assertEqual(self.histories[-1], [])

    def test_original_without_constraints_keeps_native_messages_exactly(self):
        self.executor.decide(frame(), LocalInstruction(GOAL, (), "original"))
        self.assertEqual(self.create.call_args.kwargs["messages"], self.baselines[-1])

    def test_malformed_native_output_consumes_one_request_without_entering_history(self):
        self.executor.decide(frame(), instruction())
        prior = deepcopy(self.executor._context)
        self.bindings.parse_tool_call.side_effect = ValueError("private-test-key raw image")
        with self.assertRaises(ExecutorError) as caught:
            self.executor.decide(frame(1), instruction("verification"))
        self.assertNotIn("private-test-key", str(caught.exception))
        self.assertEqual(self.create.call_count, 2)
        self.assertEqual(self.executor._context, [])

    def test_invalid_mapping_is_not_recorded_or_retried(self):
        self.bindings.map_mobile_use_action.side_effect = ValueError("out of range")
        with self.assertRaises(ExecutorError):
            self.executor.decide(frame(), instruction())
        self.create.assert_called_once()
        self.assertEqual(self.executor._context, [])

    def test_request_failure_is_sanitized_and_not_retried(self):
        self.create.side_effect = RuntimeError("private-test-key data:image/png;base64,PRIVATE")
        with self.assertRaises(ExecutorError) as caught:
            self.executor.decide(frame(), instruction())
        self.assertEqual(str(caught.exception), "GUI-Owl request failed (RuntimeError)")
        self.create.assert_called_once()
        self.assertEqual(self.executor._context, [])

    def test_non_native_responses_fail_with_one_request_each(self):
        for content, tool_calls in ((None, None), ([], None), ("", None), ("text", ["api tool call"])):
            with self.subTest(content=content, tool_calls=tool_calls):
                self.create.reset_mock()
                self.create.return_value.choices[0].message = SimpleNamespace(content=content, tool_calls=tool_calls)
                with self.assertRaises(ExecutorError):
                    self.executor.decide(frame(), instruction())
                self.create.assert_called_once()
                self.assertEqual(self.executor._context, [])


class ConfigurationAndImportsTests(unittest.TestCase):
    def test_namespace_package_is_accepted_only_with_contained_matching_locations(self):
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "resumebench"
            namespace = package / "schemas"
            namespace.mkdir(parents=True)
            foreign = Path(directory) / "other" / "resumebench" / "schemas"
            foreign.mkdir(parents=True)
            module = ModuleType("resumebench.schemas")
            module.__file__ = None
            module.__spec__ = ModuleSpec(module.__name__, loader=None, is_package=True)
            module.__path__ = [str(namespace)]
            module.__spec__.submodule_search_locations = module.__path__
            # Isolate only synthetic Benchmark imports, preserving unrelated modules.
            clean = {k: v for k, v in sys.modules.items()
                     if k != "resumebench" and not k.startswith("resumebench.")}
            with patch.dict(sys.modules, {**clean, module.__name__: module}, clear=True):
                _check_loaded_modules(package)
                for paths, locations, origin in (
                    ([str(namespace), str(foreign)], [str(namespace), str(foreign)], None),
                    ([str(namespace)], [str(foreign)], None),
                    ([], [], None),
                    ([str(namespace / "missing")], [str(namespace / "missing")], None),
                    ([str(namespace)], [str(namespace)], "foreign-origin"),
                ):
                    module.__path__ = paths
                    module.__spec__.submodule_search_locations = locations
                    module.__spec__.origin = origin
                    with self.subTest(paths=paths, locations=locations, origin=origin):
                        with self.assertRaises(BenchmarkImportError):
                            _check_loaded_modules(package)
                        self.assertIs(sys.modules[module.__name__], module)
                module.__path__ = [str(namespace)]
                module.__spec__ = None
                with self.assertRaises(BenchmarkImportError):
                    _check_loaded_modules(package)

    def test_explicit_configuration_does_not_use_environment_and_hides_key(self):
        with patch.dict(os.environ, {"GUI_OWL_MODEL": "wrong", "GUI_OWL_BASE_URL": "broken",
                                     "GUI_OWL_API_KEY": "ambient-secret"}):
            configuration = GUIOwlConfiguration(api_key="explicit-secret")
        self.assertEqual(configuration.model, "GUI-Owl-1.5-4B-Instruct")
        self.assertEqual(configuration.api_key, "explicit-secret")
        self.assertNotIn("explicit-secret", repr(configuration))

    def test_invalid_configuration_is_rejected_before_loading(self):
        for kwargs in ({"base_url": "https://user:secret@example.test"}, {"base_url": "https://example.test?key=secret"},
                       {"timeout_seconds": float("nan")}, {"timeout_seconds": True},
                       {"max_tokens": 0}, {"max_tokens": True}, {"model": "bad model"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                GUIOwlConfiguration(**kwargs)

    def test_import_and_construction_need_no_optional_dependency_or_checkout(self):
        code = '''
import builtins
import sys
original_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in {'openai', 'dotenv', 'PIL', 'resumebench', 'socket'}:
        raise AssertionError('unexpected optional import: ' + name)
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded
def audit(event, args):
    if event == 'open' and str(args[0]).endswith('.env'):
        raise AssertionError('unexpected credential read')
sys.addaudithook(audit)
from handoff_recovery.executor import GUIOwlConfiguration, GUIOwlExecutor
agent = GUIOwlExecutor('/missing/explicit/checkout', GUIOwlConfiguration())
agent.start_task('test-task', 'public task')
assert not any(name == 'resumebench' or name.startswith('resumebench.') for name in sys.modules)
'''
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_or_implicit_benchmark_root_is_rejected(self):
        for root in ("", "/nonexistent/handoffbench"):
            with self.subTest(root=root), self.assertRaises(BenchmarkImportError):
                load_gui_owl_bindings(root)

    def test_foreign_parent_child_and_package_paths_are_rejected_without_eviction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "resumebench"
            for name in ("__init__.py", "agents/gui_owl.py", "models/api_method_gui_owl.py", "devices/android.py"):
                path = package / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# placeholder must never be imported\n")
            for name, filename, paths in (
                ("resumebench", "/foreign/resumebench/__init__.py", ["/foreign/resumebench"]),
                ("resumebench.agents.gui_owl", "/foreign/resumebench/agents/gui_owl.py", None),
                ("resumebench", str(package / "__init__.py"), [str(package), "/foreign/resumebench"]),
            ):
                with self.subTest(name=name, filename=filename, paths=paths):
                    module = ModuleType(name)
                    module.__file__ = filename
                    if paths is not None:
                        module.__path__ = paths
                    with patch.dict(sys.modules, {name: module}):
                        with self.assertRaises(BenchmarkImportError):
                            load_gui_owl_bindings(root)
                        self.assertIs(sys.modules[name], module)


if __name__ == "__main__":
    unittest.main()
