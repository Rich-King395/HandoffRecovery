"""Offline protocol tests for UI-Voyager and Holo recovery adapters."""
from __future__ import annotations

from dataclasses import dataclass
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zlib

from handoff_recovery.core.types import Handoff, LocalInstruction, PublicFrame
from handoff_recovery.executor.holo import HoloExecutor, HoloConfiguration
from handoff_recovery.executor.ui_voyager import UIVoyagerConfiguration, UIVoyagerExecutor


def png_bytes():
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" +
            chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress((b"\x00" + bytes([20, 30, 40]) * 2) * 2)) +
            chunk(b"IEND", b""))


def frame(index=0):
    return PublicFrame(f"specialized_{index}", png_bytes(), 2, 2)


class UIVoyagerExecutorTests(unittest.TestCase):
    def test_native_tool_call_is_mapped_and_recovery_context_is_scoped(self):
        @dataclass
        class UIVoyagerAPIConfiguration:
            api_key: str
            model: str
            base_url: str
            timeout_seconds: float = 120.0
            max_tokens: int = 2048
        api = SimpleNamespace(UIVoyagerConfiguration=UIVoyagerAPIConfiguration,
                              request_action=Mock(return_value=(
                                  'Thought: inspect. Action: tap <tool_call>{"name":"mobile_use",'
                                  '"arguments":{"action":"click","coordinate":[500,500]}}</tool_call>')))
        bindings = SimpleNamespace(
            api=api,
            parse_tool_call=Mock(return_value={"name": "mobile_use", "arguments": {
                "action": "click", "coordinate": [500, 500]}}),
            map_mobile_use_action=Mock(return_value={"action": "tap", "coords": [1, 1]}),
            native_action_description=Mock(return_value="tap"),
            validate_action=Mock(return_value={"action": "tap", "coords": [1, 1]}),
        )
        executor = UIVoyagerExecutor("/bench", UIVoyagerConfiguration(base_url="http://127.0.0.1:8000/v1"))
        executor.start_task("task", "Set the value")
        instruction = LocalInstruction("Set the value", (), "execution",
                                       handoff=Handoff("Set the value", (), ("The value is visible",)))
        with patch("handoff_recovery.executor.ui_voyager.load_ui_voyager_bindings", return_value=bindings):
            action = executor.decide(frame(), instruction)
            executor.decide(frame(1), instruction)
        self.assertEqual(action["action"], "tap")
        self.assertIn("condition_updates to mark a condition [satisfied]",
                      api.request_action.call_args_list[0].args[1])
        self.assertEqual(api.request_action.call_args_list[0].kwargs["history"], [])
        self.assertEqual(api.request_action.call_args_list[1].kwargs["history"], ["tap"])

    def _executor_case(self, *, parse=None, map_action=None, history=None):
        @dataclass
        class UIVoyagerAPIConfiguration:
            api_key: str
            model: str
            base_url: str
            timeout_seconds: float = 120.0
            max_tokens: int = 2048
        api = SimpleNamespace(UIVoyagerConfiguration=UIVoyagerAPIConfiguration,
                              request_action=Mock(return_value="native"))
        bindings = SimpleNamespace(
            api=api,
            parse_tool_call=Mock(side_effect=parse) if isinstance(parse, BaseException)
                else Mock(return_value=parse or {"name": "mobile_use", "arguments": {"action": "click", "coordinate": [500, 500]}}),
            map_mobile_use_action=Mock(side_effect=map_action) if isinstance(map_action, BaseException)
                else Mock(return_value=map_action or {"action": "tap", "coords": [1, 1]}),
            native_action_description=Mock(side_effect=history) if isinstance(history, BaseException)
                else Mock(return_value=history or "tap"),
            validate_action=Mock(return_value={"action": "tap", "coords": [1, 1]}),
        )
        executor = UIVoyagerExecutor("/bench", UIVoyagerConfiguration())
        executor.start_task("task", "Set the value")
        return executor, bindings

    def test_error_categories_are_distinct(self):
        for field, error, expected in (
            ("parse", ValueError("malformed"), "native_parse_error"),
            ("map_action", ValueError("bad action"), "action_mapping_error"),
            ("history", RuntimeError("history"), "history_error"),
        ):
            executor, bindings = self._executor_case(**{field: error})
            with patch("handoff_recovery.executor.ui_voyager.load_ui_voyager_bindings", return_value=bindings):
                with self.assertRaises(Exception) as caught:
                    executor.decide(frame(), LocalInstruction("Set the value", (), "original"))
            self.assertEqual(caught.exception.category, expected)

    def test_request_error_is_distinct(self):
        executor, bindings = self._executor_case()
        bindings.api.request_action.side_effect = RuntimeError("transport")
        with patch("handoff_recovery.executor.ui_voyager.load_ui_voyager_bindings", return_value=bindings):
            with self.assertRaises(Exception) as caught:
                executor.decide(frame(), LocalInstruction("Set the value", (), "original"))
        self.assertEqual(caught.exception.category, "request_error")


class HoloExecutorTests(unittest.TestCase):
    def test_native_no_tool_completion_is_explicit_noop_and_keeps_history(self):
        image = SimpleNamespace(path="public.png")
        @dataclass
        class HoloAPIConfiguration:
            api_key: str
            model: str
            base_url: str
            timeout_seconds: float = 200.0
            max_tokens: int = 2048
        api = SimpleNamespace(HoloConfiguration=HoloAPIConfiguration, HISTORY_IMAGES=2,
                              encode_screenshot=Mock(return_value=image),
                              request_action=Mock(return_value=SimpleNamespace(
                                  content="The task is complete.", reasoning="", tool_calls=[])))
        parsed = SimpleNamespace(call=None, content="The task is complete.", reasoning="")
        bindings = SimpleNamespace(api=api, parse_completion=Mock(return_value=parsed),
                                   map_holo_action=Mock(), validate_action=Mock())
        executor = HoloExecutor("/bench", HoloConfiguration(base_url="http://127.0.0.1:8003/v1"))
        executor.start_task("task", "Save the value")
        instruction = LocalInstruction("Save the value", (), "original")
        with patch("handoff_recovery.executor.holo.load_holo_bindings", return_value=bindings):
            result = executor.decide(frame(), instruction)
            executor.decide(frame(1), instruction)
        self.assertEqual(type(result).__name__, "ExecutorNoOp")
        self.assertEqual(len(api.request_action.call_args_list[1].kwargs["history"]), 1)

    def test_function_call_is_mapped_and_native_history_is_retained(self):
        image = SimpleNamespace(path="public.png")
        @dataclass
        class HoloAPIConfiguration:
            api_key: str
            model: str
            base_url: str
            timeout_seconds: float = 200.0
            max_tokens: int = 2048
        api = SimpleNamespace(HoloConfiguration=HoloAPIConfiguration, HISTORY_IMAGES=2,
                              encode_screenshot=Mock(return_value=image),
                              request_action=Mock(return_value=SimpleNamespace(
                                  content="", reasoning="", tool_calls=[])))
        call = {"name": "click", "arguments": {"element": "Save", "x": 500, "y": 500}}
        parsed = SimpleNamespace(call=call, content="", reasoning="")
        bindings = SimpleNamespace(
            api=api, parse_completion=Mock(return_value=parsed),
            map_holo_action=Mock(return_value={"action": "tap", "coords": [1, 1], "target": "Save"}),
            validate_action=Mock(return_value={"action": "tap", "coords": [1, 1]}),
        )
        executor = HoloExecutor("/bench", HoloConfiguration(base_url="http://127.0.0.1:8003/v1"))
        executor.start_task("task", "Save the value")
        instruction = LocalInstruction("Save the value", (), "original")
        with patch("handoff_recovery.executor.holo.load_holo_bindings", return_value=bindings):
            action = executor.decide(frame(), instruction)
            executor.decide(frame(1), instruction)
        self.assertEqual(action["action"], "tap")
        self.assertEqual(len(api.request_action.call_args_list[1].kwargs["history"]), 1)
        self.assertEqual(api.request_action.call_args_list[1].kwargs["history"][0]["assistant"]["tool_calls"][0]["function"], call)

    def _executor_case(self, *, parse=None, map_action=None, validate=None):
        image = SimpleNamespace(path="public.png")
        @dataclass
        class HoloAPIConfiguration:
            api_key: str
            model: str
            base_url: str
            timeout_seconds: float = 200.0
            max_tokens: int = 2048
        api = SimpleNamespace(
            HoloConfiguration=HoloAPIConfiguration,
            HISTORY_IMAGES=2, encode_screenshot=Mock(return_value=image),
            request_action=Mock(return_value=SimpleNamespace(content="", reasoning="", tool_calls=[])),
        )
        parsed = parse if parse is not None else SimpleNamespace(
            call={"name": "click", "arguments": {"element": "Save", "x": 500, "y": 500}},
            content="", reasoning="")
        bindings = SimpleNamespace(
            api=api, parse_completion=Mock(side_effect=parsed) if isinstance(parsed, BaseException)
                else Mock(return_value=parsed),
            map_holo_action=Mock(side_effect=map_action) if isinstance(map_action, BaseException)
                else Mock(return_value=map_action or {"action": "tap", "coords": [1, 1]}),
            validate_action=Mock(side_effect=validate) if isinstance(validate, BaseException)
                else Mock(return_value=validate or {"action": "tap", "coords": [1, 1]}),
        )
        executor = HoloExecutor("/bench", HoloConfiguration())
        executor.start_task("task", "Save the value")
        return executor, bindings

    def test_error_categories_are_distinct(self):
        for field, error, expected in (
            ("parse", ValueError("malformed"), "native_parse_error"),
            ("map_action", ValueError("bad action"), "action_mapping_error"),
            ("validate", ValueError("invalid action"), "action_mapping_error"),
        ):
            executor, bindings = self._executor_case(**{field: error})
            with patch("handoff_recovery.executor.holo.load_holo_bindings", return_value=bindings):
                with self.assertRaises(Exception) as caught:
                    executor.decide(frame(), LocalInstruction("Save the value", (), "original"))
            self.assertEqual(caught.exception.category, expected)

    def test_request_error_is_distinct(self):
        executor, bindings = self._executor_case()
        bindings.api.request_action.side_effect = RuntimeError("transport")
        with patch("handoff_recovery.executor.holo.load_holo_bindings", return_value=bindings):
            with self.assertRaises(Exception) as caught:
                executor.decide(frame(), LocalInstruction("Save the value", (), "original"))
        self.assertEqual(caught.exception.category, "request_error")


if __name__ == "__main__":
    unittest.main()
