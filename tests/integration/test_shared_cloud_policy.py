"""Offline mock-chain checks for the shared HandoffBench cloud policy."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
import struct
import unittest
from unittest.mock import patch
import zlib

from handoff_recovery.core.types import Handoff, LocalInstruction, PublicFrame
from handoff_recovery.executor.cloud import CloudExecutor, CloudExecutorConfiguration
from handoff_recovery.integrations.handoffbench import load_cloud_bindings


def _png_bytes(width=56, height=112):
    def chunk(kind, body):
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\x00" + bytes([32, 64, 96]) * width) * height))
            + chunk(b"IEND", b""))


@unittest.skipUnless(os.environ.get("HANDOFFBENCH_ROOT"),
                     "Set HANDOFFBENCH_ROOT for shared cloud policy tests")
class SharedCloudPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(os.environ["HANDOFFBENCH_ROOT"]).resolve()
        cls.bindings = load_cloud_bindings(cls.root, "qwen")
        cls.api = cls.bindings.api

    def _executor(self):
        executor = CloudExecutor(
            self.root,
            CloudExecutorConfiguration("qwen", "mock-key", "mock-qwen", ""),
        )
        executor.start_task("mock-task", "Set the sample value and preserve its label.")
        return executor

    def test_baseline_hash_is_unchanged_and_recovery_context_is_added_only_when_present(self):
        goal = "Set the sample value and preserve its label."
        frame = PublicFrame("screen-1", _png_bytes(), 56, 112)
        calls = []

        def request_action(image, request_goal, width, height, **kwargs):
            calls.append((request_goal, kwargs.get("history"), kwargs["configuration"]))
            return '{"action":"tap","coords":[500,500]}'

        executor = self._executor()
        original = LocalInstruction(goal, ("Preserve the existing label.",), "original")
        handoff = LocalInstruction(
            goal, (), "execution",
            handoff=Handoff("Set the remaining value.", ("Preserve the label",),
                            ("The value is visible",)),
        )
        configuration = self.api.QwenConfiguration(
            api_key="mock-key", model="mock-qwen", base_url="",
        )
        validate = lambda: configuration
        executor._bindings = replace(self.bindings, validate_configuration=validate)
        with patch.object(self.api, "request_action", side_effect=request_action):
            executor.decide(frame, original)
            executor.decide(PublicFrame("screen-2", _png_bytes(56, 112), 56, 112), handoff)

        self.assertEqual(calls[0][0], goal)
        self.assertEqual(calls[0][1], [])
        self.assertEqual(calls[0][2].model, "mock-qwen")
        model_width, model_height = self.bindings.image_size(56, 112)
        gui_policy = __import__("resumebench.models.gui_policy", fromlist=["action_prompt"])
        expected_hash = hashlib.sha256(gui_policy.action_prompt(
            goal, model_width, model_height, [], coordinate_space="normalized_1000",
        ).encode("utf-8")).hexdigest()
        # The baseline request still uses the exact HandoffBench prompt input.
        actual_hash = hashlib.sha256(gui_policy.action_prompt(
            calls[0][0], model_width, model_height, calls[0][1],
            coordinate_space="normalized_1000",
        ).encode("utf-8")).hexdigest()
        self.assertEqual(actual_hash, expected_hash)

        recovery_goal, recovery_history, _ = calls[1]
        self.assertIn("Recovery context", recovery_goal)
        self.assertIn('"scope": "execution"', recovery_goal)
        self.assertIn('"remaining_objective": "Set the remaining value."', recovery_goal)
        # Recovery scopes receive structured context separately; their native
        # action history starts fresh instead of mixing pre-handoff actions into
        # the shared policy history.
        self.assertEqual(recovery_history, [])

    def test_descriptor_reports_shared_protocol_without_xml_fallback(self):
        from dataclasses import replace

        configuration = self.api.QwenConfiguration(
            api_key="mock-key", model="mock-qwen", base_url="",
        )
        executor = self._executor()
        executor._bindings = replace(self.bindings,
                                     validate_configuration=lambda: configuration)
        descriptor = executor.descriptor
        self.assertEqual(descriptor["protocol"], "unique_json_action_v2")
        self.assertEqual(descriptor["configuration_source"],
                         "handoffbench_provider_validate_configuration")
        self.assertEqual(descriptor["transport"], "handoffbench_shared_cloud_agent")
        self.assertFalse(descriptor["legacy_path_enabled"])
        self.assertEqual(descriptor["recovery_context"],
                         "optional_for_execution_verification_fallback_only")
        self.assertEqual(descriptor["model"], "mock-qwen")


if __name__ == "__main__":
    unittest.main()
