"""Offline tests for the HandoffBench shared cloud Executor adapter."""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
import struct
import unittest
from unittest.mock import Mock, patch
import zlib

from handoff_recovery.core.types import Handoff, LocalInstruction, PublicFrame, VerificationTask
from handoff_recovery.executor.cloud import (
    CloudExecutor, CloudExecutorConfiguration, _parse_qwen_computer_use,
)


def png_bytes(width=2, height=2):
    def chunk(kind, body):
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\x00" + bytes([32, 64, 96]) * width) * height))
            + chunk(b"IEND", b""))


def frame(index=0):
    return PublicFrame(f"screen_{index}", png_bytes(), 2, 2)


class CloudExecutorTests(unittest.TestCase):
    def setUp(self):
        @dataclass
        class QwenConfiguration:
            api_key: str
            model: str
            base_url: str
        self.api = SimpleNamespace(
            request_action=Mock(return_value='{"action":"tap","coords":[500,500]}'),
            QwenConfiguration=QwenConfiguration,
        )
        self.bindings = SimpleNamespace(
            api=self.api,
            parse_action=Mock(side_effect=lambda raw: {"action": "tap", "coords": [500, 500]}),
            map_action=Mock(return_value={"action": "tap", "coords": [1, 1]}),
            image_size=Mock(return_value=(2, 2)),
            coordinate_space="normalized_1000",
            provider="qwen",
            validate_action=Mock(return_value={"action": "tap", "coords": [1, 1]}),
        )
        self.executor = CloudExecutor(
            "/explicit/benchmark",
            CloudExecutorConfiguration("qwen", "private-key", "qwen-test", ""),
            allow_legacy_path=True,
        )
        self.executor.start_task("task-1", "Set the requested value.")

    def instruction(self, scope="execution"):
        return LocalInstruction(
            "Set the requested value.", (), scope,
            verification=(VerificationTask("Read the value.", ("Value is visible",), ())
                          if scope == "verification" else None),
            handoff=(Handoff("Set the requested value.", (), ("The value is visible",))
                     if scope == "execution" else None),
        )

    def test_scoped_context_and_history_are_sent_once(self):
        with patch("handoff_recovery.executor.cloud.load_cloud_bindings",
                   return_value=self.bindings):
            first = self.executor.decide(frame(), self.instruction())
            second = self.executor.decide(frame(1), self.instruction())
        self.assertEqual(first, {"action": "tap", "coords": [1, 1]})
        self.assertEqual(second, {"action": "tap", "coords": [1, 1]})
        self.assertEqual(self.api.request_action.call_count, 2)
        first_goal = self.api.request_action.call_args_list[0].args[1]
        self.assertIn('"scope": "execution"', first_goal)
        self.assertEqual(self.api.request_action.call_args_list[0].kwargs["history"], [])
        self.assertEqual(len(self.api.request_action.call_args_list[1].kwargs["history"]), 1)

    def test_fallback_restores_original_history(self):
        original = LocalInstruction("Set the requested value.", (), "original")
        fallback = LocalInstruction("Set the requested value.", (), "fallback",
                                    resume_original=True, resume_reason="planner_failed")
        with patch("handoff_recovery.executor.cloud.load_cloud_bindings",
                   return_value=self.bindings):
            self.executor.decide(frame(), original)
            self.executor.decide(frame(1), fallback)
        self.assertEqual(self.api.request_action.call_args_list[1].kwargs["history"], [
            {"action": "tap", "coords": [500, 500]},
        ])
        self.assertIn("resume_original_context", self.api.request_action.call_args_list[1].args[1])

    def test_request_failure_is_classified_without_raw_error(self):
        self.api.request_action.side_effect = RuntimeError("private-key and image bytes")
        with patch("handoff_recovery.executor.cloud.load_cloud_bindings",
                   return_value=self.bindings):
            with self.assertRaises(Exception) as caught:
                self.executor.decide(frame(), self.instruction())
        self.assertEqual(caught.exception.category, "request_error")
        self.assertNotIn("private-key", str(caught.exception))

    def test_qwen_computer_use_xml_is_normalized(self):
        raw = ("<tool_call><function=computer_use>"
               "<parameter=action>tap</parameter>"
               "<parameter=target>settings button</parameter>"
               "<parameter=coords>[820, 54]</parameter>"
               "</function></tool_call>")
        self.assertEqual(_parse_qwen_computer_use(raw), {
            "action": "tap", "target": "settings button", "coords": [820, 54],
        })

    def test_legacy_path_is_rejected_by_default(self):
        executor = CloudExecutor(
            "/explicit/benchmark",
            CloudExecutorConfiguration("qwen", "private-key", "qwen-test", ""),
        )
        executor.start_task("task-legacy-guard", "Set the requested value.")
        with patch("handoff_recovery.executor.cloud.load_cloud_bindings",
                   return_value=self.bindings):
            with self.assertRaises(Exception) as caught:
                executor.decide(frame(), self.instruction())
        self.assertEqual(caught.exception.category, "integration_error")

    def test_qwen_xml_fallback_reaches_shared_mapper(self):
        self.bindings.parse_action.side_effect = ValueError("JSON expected")
        self.api.request_action.return_value = (
            "<tool_call><function=computer_use>"
            "<parameter=action>tap</parameter>"
            "<parameter=coords>[500,500]</parameter>"
            "</function></tool_call>"
        )
        with patch("handoff_recovery.executor.cloud.load_cloud_bindings",
                   return_value=self.bindings):
            result = self.executor.decide(frame(), self.instruction())
        self.assertEqual(result, {"action": "tap", "coords": [1, 1]})
        self.assertEqual(self.bindings.map_action.call_args.args[0], {
            "action": "tap", "coords": [500, 500],
        })


if __name__ == "__main__":
    unittest.main()
