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
            calls.append((request_goal, kwargs.get("history"), kwargs["configuration"],
                          kwargs.get("completion_conditions"), kwargs.get("constraints")))
            return json.dumps({"action": "tap", "coords": [500, 500],
                               "target": f"own-action-{len(calls)}"})

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
            executor.decide(PublicFrame("screen-3", _png_bytes(56, 112), 56, 112), handoff)

        self.assertEqual(len(calls), 3)
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

        recovery_goal, recovery_history, _, completion_conditions, constraints = calls[1]
        self.assertIn(goal, recovery_goal)
        self.assertIn("Set the remaining value.", recovery_goal)
        self.assertEqual(recovery_goal.count("- [pending] The value is visible"), 1)
        self.assertIsNone(completion_conditions)
        self.assertIsNone(constraints)
        self.assertNotIn("Preserve the label", recovery_goal)
        self.assertNotIn("Last action feedback", recovery_goal)
        # Isolated recovery starts empty, then retains only its own public actions.
        self.assertEqual(recovery_history, [])
        self.assertEqual(calls[2][0], recovery_goal)
        self.assertEqual(calls[2][1], [{"action": "tap", "target": "own-action-2"}])
        recovery_prompt = gui_policy.action_prompt(
            recovery_goal, model_width, model_height, recovery_history,
            coordinate_space="normalized_1000",
            completion_conditions=completion_conditions,
            constraints=constraints,
            recovery_scope="execution",
        )
        self.assertEqual(recovery_prompt.count("- [pending] The value is visible"), 1)
        self.assertNotIn("own-action-1", recovery_prompt)
        continued_prompt = gui_policy.action_prompt(
            calls[2][0], model_width, model_height, calls[2][1],
            coordinate_space="normalized_1000", recovery_scope="execution",
        )
        self.assertIn("Your previous actions in this task", continued_prompt)
        self.assertIn('"action": "tap"', continued_prompt)
        self.assertIn("own-action-2", continued_prompt)
        self.assertNotIn("own-action-1", continued_prompt)

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
