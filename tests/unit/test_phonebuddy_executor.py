"""Offline PhoneBuddy executor contract tests."""
from __future__ import annotations

from copy import deepcopy
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zlib

from handoff_recovery.core.types import Handoff, LocalInstruction, PublicFrame, VerificationTask
from handoff_recovery.executor.phonebuddy import (
    PhoneBuddyConfiguration, PhoneBuddyExecutor,
)


def png_bytes():
    def chunk(kind, payload):
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\x00" + bytes([32, 64, 96]) * 2) * 2))
            + chunk(b"IEND", b""))


def frame(index=0):
    return PublicFrame(f"screen_{index}", png_bytes(), 2, 2)


def instruction(scope="execution", *, resume_original=False):
    return LocalInstruction(
        "Finish the file edit.", ("Keep the other file unchanged.",), scope,
        verification=(VerificationTask("Inspect the target state.", ("The target is visible.",), ("Do not edit the target.",))
                      if scope == "verification" else None),
        handoff=(Handoff("Open the target and save the exact text.",
                         ("Keep the other file unchanged.",), ("The target text is saved.",))
                 if scope == "execution" else None),
        resume_original=resume_original,
        resume_reason=("planner_unavailable_or_invalid" if resume_original else None),
    )


class PhoneBuddyExecutorTests(unittest.TestCase):
    def setUp(self):
        self.build_calls = []
        self.history_calls = []
        self.bindings = SimpleNamespace(
            api=SimpleNamespace(
                build_messages=Mock(side_effect=self.build_messages),
                TEMPERATURE=0.0, TOP_P=1.0, TOP_K=-1,
            ),
            parse_tool_call=Mock(return_value=(
                {"name": "button_press", "arguments": {"type": "back"}}, "go back")),
            map_phonebuddy_action=Mock(return_value={"action": "back", "coords": []}),
            validate_action=Mock(return_value={"action": "back", "coords": []}),
            history_entry=Mock(side_effect=self.history_entry),
        )
        self.response = SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content='<tool_call>{"name":"button_press","arguments":{"type":"back"}}</tool_call>',
                                    reasoning_content=None, reasoning=None))])
        self.client = Mock()
        self.client.chat.completions.create.return_value = self.response
        manager = Mock()
        manager.__enter__ = Mock(return_value=self.client)
        manager.__exit__ = Mock(return_value=False)
        self.factory = Mock(return_value=manager)
        self.executor = PhoneBuddyExecutor(
            "/explicit/benchmark",
            PhoneBuddyConfiguration(api_key="private-test-key"),
            client_factory=self.factory,
        )
        self.executor.start_task("task-1", "Finish the file edit.")

    def build_messages(self, image, goal, history, *, prompt_path):
        self.build_calls.append((image, goal, deepcopy(history), prompt_path))
        return [{"role": "system", "content": "native"},
                {"role": "user", "content": [{"type": "text", "text": goal}]}]

    def history_entry(self, call, thought):
        self.history_calls.append((call, thought))
        return {"thought": thought, "action_str": "button_press({\"type\":\"back\"})"}

    def test_native_request_and_history_are_preserved(self):
        with patch("handoff_recovery.executor.phonebuddy.load_phonebuddy_bindings",
                   return_value=self.bindings):
            action = self.executor.decide(frame(), instruction())
        self.assertEqual(action, {"action": "back", "coords": [], "thought": "go back"})
        self.assertEqual(len(self.build_calls), 1)
        self.assertEqual(self.build_calls[0][2], [])
        self.factory.assert_called_once_with(api_key="private-test-key",
            base_url="http://127.0.0.1:8001/v1", timeout=180.0, max_retries=0)
        args = self.client.chat.completions.create.call_args.kwargs
        self.assertEqual(args["model"], "PhoneBuddy-4B")
        self.assertEqual(args["max_completion_tokens"], 8192)
        self.assertEqual(args["extra_body"]["chat_template_kwargs"], {"enable_thinking": True})

        with patch("handoff_recovery.executor.phonebuddy.load_phonebuddy_bindings",
                   return_value=self.bindings):
            self.executor.decide(frame(1), instruction())
        self.assertEqual(len(self.build_calls[1][2]), 1)
        self.assertEqual(self.history_calls[0][1], "go back")
        self.bindings.validate_action.assert_called()

    def test_production_path_delegates_request_to_handoffbench_api(self):
        self.bindings.request_action = Mock(return_value=
            '<tool_call>{"name":"button_press","arguments":{"type":"back"}}</tool_call>')
        executor = PhoneBuddyExecutor(
            "/explicit/benchmark", PhoneBuddyConfiguration(api_key="private-test-key"))
        executor.start_task("task-api", "Finish the file edit.")
        with patch("handoff_recovery.executor.phonebuddy.load_phonebuddy_bindings",
                   return_value=self.bindings):
            action = executor.decide(frame(), instruction("original"))
        self.assertEqual(action["action"], "back")
        self.bindings.request_action.assert_called_once()
        request = self.bindings.request_action.call_args
        self.assertEqual(request.args[1], "Finish the file edit.")
        self.assertEqual(request.kwargs["history"], [])
        self.assertIsNone(request.kwargs["recovery_context"])

    def test_production_path_passes_structured_recovery_context(self):
        self.bindings.request_action = Mock(return_value=
            '<tool_call>{"name":"button_press","arguments":{"type":"back"}}</tool_call>')
        executor = PhoneBuddyExecutor(
            "/explicit/benchmark", PhoneBuddyConfiguration(api_key="private-test-key"))
        executor.start_task("task-api-context", "Finish the file edit.")
        with patch("handoff_recovery.executor.phonebuddy.load_phonebuddy_bindings",
                   return_value=self.bindings):
            executor.decide(frame(), instruction("execution"))
        context = self.bindings.request_action.call_args.kwargs["recovery_context"]
        self.assertEqual(context["scope"], "execution")
        self.assertIn("handoff", context)

    def test_phonebuddy_descriptor_keeps_native_protocol_explicit(self):
        descriptor = self.executor.descriptor
        self.assertEqual(descriptor["native_protocol"], "phonebuddy_tool_call_v1")
        self.assertEqual(descriptor["action_normalization"],
                         "native_parser_then_handoffbench_mapper")

    def test_native_context_is_reset_when_scope_changes(self):
        with patch("handoff_recovery.executor.phonebuddy.load_phonebuddy_bindings",
                   return_value=self.bindings):
            self.executor.decide(frame(), instruction("execution"))
            self.executor.decide(frame(1), instruction("execution"))
            self.executor.decide(frame(2), instruction("verification"))
            self.executor.decide(frame(3), instruction("verification"))
        self.assertEqual([len(call[2]) for call in self.build_calls], [0, 1, 0, 1])
        self.assertEqual(len(self.executor._context), 2)

    def test_fallback_restores_original_native_context(self):
        with patch("handoff_recovery.executor.phonebuddy.load_phonebuddy_bindings",
                   return_value=self.bindings):
            self.executor.decide(frame(), instruction("original"))
            self.executor.decide(frame(1), instruction("original"))
            self.executor.decide(frame(2), instruction("execution"))
            self.executor.decide(frame(3), instruction("fallback", resume_original=True))
        # Recovery starts isolated; fallback then restores the original
        # snapshot with its two successful native actions.
        self.assertEqual([len(call[2]) for call in self.build_calls], [0, 1, 0, 2])
        prompt = self.client.chat.completions.create.call_args.kwargs["messages"][-1]["content"][-1]["text"]
        self.assertEqual(prompt, "Finish the file edit.")
        self.assertEqual(self.executor._context_scope, "original")

    def test_execution_context_requires_self_check_before_done(self):
        self.executor = PhoneBuddyExecutor(
            "/explicit/benchmark",
            PhoneBuddyConfiguration(api_key="private-test-key", enable_completion_check=True),
            client_factory=self.factory,
        )
        self.executor.start_task("task-1", "Finish the file edit.")
        with patch("handoff_recovery.executor.phonebuddy.load_phonebuddy_bindings",
                   return_value=self.bindings):
            self.executor.decide(frame(), instruction("execution"))
        prompt = self.client.chat.completions.create.call_args.kwargs["messages"][-1]["content"][-1]["text"]
        self.assertIn("condition_updates to mark a condition [satisfied]", prompt)
        self.assertIn("Return done only when every condition in Completion condition status is marked [satisfied]", prompt)
        self.assertIn("Open the target and save the exact text", prompt)
        self.assertIn("completion_check item", prompt)

    def test_completion_check_can_be_disabled_for_control_ab(self):
        configuration = PhoneBuddyConfiguration(
            api_key="private-test-key", enable_completion_check=False)
        executor = PhoneBuddyExecutor(
            "/explicit/benchmark", configuration, client_factory=self.factory)
        executor.start_task("task-control", "Finish the file edit.")
        with patch("handoff_recovery.executor.phonebuddy.load_phonebuddy_bindings",
                   return_value=self.bindings):
            executor.decide(frame(), instruction())
        prompt = self.client.chat.completions.create.call_args.kwargs["messages"][-1]["content"][-1]["text"]
        self.assertFalse(executor.requires_completion_check)
        self.assertNotIn("completion_check array", prompt)

    def test_finish_completion_check_is_preserved_after_native_mapping(self):
        check = [
            {"condition_id": "c1", "status": "satisfied", "evidence": "Saved target is visible."},
        ]
        self.bindings.parse_tool_call.return_value = ({
            "name": "finish",
            "arguments": {"text": "done", "completion_check": check},
        }, "verified")
        self.bindings.map_phonebuddy_action.return_value = {
            "action": "done", "coords": [], "reason": "done",
        }
        with patch("handoff_recovery.executor.phonebuddy.load_phonebuddy_bindings",
                   return_value=self.bindings):
            action = self.executor.decide(frame(), instruction())
        self.assertEqual(action["action"], "done")
        self.assertEqual(action["completion_check"], check)
        mapped_call = self.bindings.map_phonebuddy_action.call_args.args[0]
        self.assertEqual(mapped_call["arguments"], {"text": "done"})
        self.assertEqual(self.history_calls[0][0]["arguments"],
                         {"text": "done", "completion_check": check})

    def test_execution_context_does_not_include_prior_progress(self):
        local = LocalInstruction(
            "Finish the file edit.", (), "execution", handoff=Handoff(
                "Set only the incorrect time", (), ("09:00 visible",)),
            )
        with patch("handoff_recovery.executor.phonebuddy.load_phonebuddy_bindings",
                   return_value=self.bindings):
            self.executor.decide(frame(), local)
        prompt = self.client.chat.completions.create.call_args.kwargs["messages"][-1]["content"][-1]["text"]
        self.assertNotIn("prior_progress", prompt)
        self.assertIn("Set only the incorrect time", prompt)

    def test_configuration_rejects_invalid_model(self):
        with self.assertRaises(ValueError):
            PhoneBuddyConfiguration(model="bad model")

    def test_parse_and_mapping_failures_have_distinct_categories(self):
        with patch("handoff_recovery.executor.phonebuddy.load_phonebuddy_bindings",
                   return_value=self.bindings):
            self.bindings.parse_tool_call.side_effect = ValueError("malformed")
            with self.assertRaises(Exception) as caught:
                self.executor.decide(frame(), instruction())
        self.assertEqual(caught.exception.category, "native_parse_error")
        self.assertEqual(caught.exception.cause_type, "ValueError")

        self.setUp()
        with patch("handoff_recovery.executor.phonebuddy.load_phonebuddy_bindings",
                   return_value=self.bindings):
            self.bindings.map_phonebuddy_action.side_effect = ValueError("unsupported")
            with self.assertRaises(Exception) as caught:
                self.executor.decide(frame(), instruction())
        self.assertEqual(caught.exception.category, "action_mapping_error")
        self.assertIn("unsupported", str(caught.exception))

    def test_recovery_forwards_flat_points_to_the_strict_native_mapper_once(self):
        call = {"name": "click", "arguments": {"points": [500, 900]}}
        self.bindings.parse_tool_call.return_value = (call, "click")
        self.bindings.map_phonebuddy_action.side_effect = ValueError("native nested points required")
        with patch("handoff_recovery.executor.phonebuddy.load_phonebuddy_bindings", return_value=self.bindings):
            with self.assertRaises(Exception) as caught:
                self.executor.decide(frame(), instruction())
        self.assertEqual(caught.exception.category, "action_mapping_error")
        self.bindings.map_phonebuddy_action.assert_called_once_with(call, (2, 2))
        self.assertEqual(self.executor._context, [])

    def test_recovery_preserves_native_nested_phonebuddy_points(self):
        call = {"name": "click", "arguments": {"points": [[500, 900]]}}
        self.bindings.parse_tool_call.return_value = (call, "click")
        with patch("handoff_recovery.executor.phonebuddy.load_phonebuddy_bindings", return_value=self.bindings):
            self.executor.decide(frame(), instruction())
        self.bindings.map_phonebuddy_action.assert_called_once_with(call, (2, 2))
        self.assertEqual(call["arguments"]["points"], [[500, 900]])

    def test_request_and_response_failures_have_distinct_categories(self):
        with patch("handoff_recovery.executor.phonebuddy.load_phonebuddy_bindings",
                   return_value=self.bindings):
            self.client.chat.completions.create.side_effect = RuntimeError("secret")
            with self.assertRaises(Exception) as caught:
                self.executor.decide(frame(), instruction())
        self.assertEqual(caught.exception.category, "request_error")
        self.assertEqual(str(caught.exception), "PhoneBuddy request failed (RuntimeError)")

        self.setUp()
        self.response.choices = []
        with patch("handoff_recovery.executor.phonebuddy.load_phonebuddy_bindings",
                   return_value=self.bindings):
            with self.assertRaises(Exception) as caught:
                self.executor.decide(frame(), instruction())
        self.assertEqual(caught.exception.category, "response_error")

    def test_prompt_and_history_failures_have_distinct_categories(self):
        self.bindings.api.build_messages.side_effect = ValueError("prompt")
        with patch("handoff_recovery.executor.phonebuddy.load_phonebuddy_bindings",
                   return_value=self.bindings):
            with self.assertRaises(Exception) as caught:
                self.executor.decide(frame(), instruction())
        self.assertEqual(caught.exception.category, "prompt_error")

        self.setUp()
        self.bindings.history_entry.side_effect = RuntimeError("history")
        with patch("handoff_recovery.executor.phonebuddy.load_phonebuddy_bindings",
                   return_value=self.bindings):
            with self.assertRaises(Exception) as caught:
                self.executor.decide(frame(), instruction())
        self.assertEqual(caught.exception.category, "history_error")


if __name__ == "__main__":
    unittest.main()
