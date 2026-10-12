"""Offline coordinator contracts; no cloud, device, or Benchmark dependency."""

from dataclasses import replace
import json
import struct
import unittest
import zlib

from handoff_recovery.core.coordinator import LifecycleError, RecoveryCoordinator
from handoff_recovery.core.types import (
    HANDOFFBENCH_ACTIONS, Handoff, OwnAction, Plan, PlanReply, PlanRequest, PublicFrame, SlimPlan,
    RecoveryBudget, TOTAL_DECISIONS, Unknown, VerificationTask,
)
from handoff_recovery.planner.validation import PlanValidationError


GOAL = "Set Test Contact's email to test@example.invalid and retain its phone."
CONSTRAINTS = ("Retain the existing phone and all other contacts.",)


def frame(evidence_id, color=(32, 64, 96)):
    """Create a complete tiny RGB PNG using only the Python standard library."""
    def chunk(kind, payload):
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0)
    pixels = (b"\x00" + bytes(color) * 2) * 2
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
           + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))
    return PublicFrame(evidence_id, png, 2, 2)


def execute_reply(**kwargs):
    return PlanReply(Plan(
        "EXECUTE", "The remaining email edit is visible.",
        handoff=Handoff(
            "Fill the original requested email and save the same contact.",
            CONSTRAINTS,
            ("The contact details visibly show the requested saved email.",),
        ),
        use_prior_progress=True,
    ), **kwargs)


def verify_reply():
    return PlanReply(Plan(
        "VERIFY", "The current contact details need inspection.",
        unknowns=(Unknown("What email is currently saved?", True,
                          "The list does not show the email."),),
        verification=VerificationTask(
            "Open the contact details and inspect the email.",
            ("A visible contact details screen showing its email.",),
            ("Read only; do not change or save business data.",),
        ),
    ))


def fallback_reply():
    return PlanReply(Plan("FALLBACK", "The evidence cannot support reliable guidance."))


class FakePlanner:
    def __init__(self, replies=()):
        self.replies = list(replies)
        self.requests = []

    def plan(self, request):
        self.requests.append(request)
        if not self.replies:
            raise AssertionError("Unexpected cloud planning call")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def slim_execute_reply():
    return PlanReply(SlimPlan(
        "EXECUTE", "Fill the requested email and save the same contact.",
        ("The saved contact visibly shows the requested email.",),
        CONSTRAINTS,
    ))


def slim_verify_reply():
    return PlanReply(SlimPlan(
        "VERIFY", "Open the contact details and inspect the saved email.",
        ("A visible contact details screen showing the email.",),
        ("Read only; do not change or save business data.",),
    ))


class FakeExecutor:
    def __init__(self, actions, *, requires_completion_check=False):
        self.actions = list(actions)
        self.starts = []
        self.requests = []
        self.requires_completion_check = requires_completion_check

    def start_task(self, task_id, goal):
        self.starts.append((task_id, goal))

    def decide(self, observation, instruction):
        self.requests.append((observation, instruction))
        if not self.actions:
            raise AssertionError("Unexpected extra local decision")
        action = self.actions.pop(0)
        if isinstance(action, Exception):
            raise action
        return action


class CoordinatorTests(unittest.TestCase):
    def make_coordinator(self, actions, replies=(), *, requires_completion_check=False, budget=None):
        planner = FakePlanner(replies)
        executor = FakeExecutor(actions, requires_completion_check=requires_completion_check)
        coordinator = RecoveryCoordinator(
            task_id="synthetic-contact", original_goal=GOAL,
            public_constraints=CONSTRAINTS, planner=planner, executor=executor,
            budget=budget,
        )
        return coordinator, planner, executor

    @staticmethod
    def completion_check(*, status="satisfied"):
        return [{"condition_id": "c1", "status": status,
                 "evidence": "The saved email is visible."}]

    def return_control(self, coordinator, evidence_id="returned"):
        coordinator.pause_for_handoff()
        observation = frame(evidence_id)
        coordinator.return_control(observation)
        return observation

    def run_prefix(self, coordinator, count):
        for index in range(count):
            result = coordinator.step(frame(f"prefix_{index}"))
            self.assertEqual(result.kind, "action")
            self.assertIsNone(coordinator.acknowledge_action(success=True))

    def test_accepts_the_complete_handoffbench_action_vocabulary(self):
        actions = [
            {"action": "double_tap"}, {"action": "drag"}, {"action": "close_app"},
        ]
        coordinator, _, _ = self.make_coordinator(actions)
        for index, expected in enumerate(("double_tap", "drag", "close_app")):
            result = coordinator.step(frame(f"complete_actions_{index}"))
            self.assertEqual(result.kind, "action")
            self.assertEqual(result.action["action"], expected)
            coordinator.acknowledge_action(success=True)
        self.assertTrue(HANDOFFBENCH_ACTIONS.issuperset({
            "tap", "text", "double_tap", "swipe", "drag", "long_press",
            "keyevent", "back", "home", "wait", "launch_app", "close_app",
            "done", "fail",
        }))

    def test_native_execution_never_calls_cloud_without_control_return(self):
        coordinator, planner, executor = self.make_coordinator(
            [{"action": "wait"}, {"action": "done"}])
        self.run_prefix(coordinator, 1)
        result = coordinator.step(frame("native_done"))
        self.assertEqual((result.kind, result.scope), ("task_complete", "original"))
        self.assertEqual(coordinator.decisions_used, 2)
        self.assertEqual(planner.requests, [])
        self.assertEqual(executor.starts, [("synthetic-contact", GOAL)])
        self.assertIsNone(coordinator.report()["task_success"])

    def test_execution_instruction_contains_compact_prior_progress(self):
        coordinator, _, executor = self.make_coordinator(
            [{"action": "tap", "coords": [1, 1]}, {"action": "tap", "coords": [2, 2]}],
            [execute_reply()])
        self.run_prefix(coordinator, 1)
        self.return_control(coordinator)
        result = coordinator.step(frame("execution_1"))
        self.assertEqual(result.kind, "action")
        self.assertEqual(executor.requests[-1][1].prior_progress,
                         {
                             "purpose": "orientation_only",
                             "reliability": "unverified",
                             "current_state_source": "latest_screenshot",
                             "attempt_summary": {
                                 "decision_count": 1,
                                 "action_types_attempted": ["tap"],
                             },
                             "interpretation_rule": (
                                 "These are attempted actions only. They do not prove that any action "
                                 "succeeded or describe the current UI state."
                             ),
                         })
        coordinator.acknowledge_action(success=True)

    def test_execution_omits_prior_progress_without_planner_opt_in(self):
        coordinator, _, executor = self.make_coordinator(
            [{"action": "tap", "coords": [1, 1]}, {"action": "wait"}],
            [slim_execute_reply()])
        self.run_prefix(coordinator, 1)
        self.return_control(coordinator)
        result = coordinator.step(frame("execution_without_progress"))
        self.assertEqual(result.kind, "action")
        self.assertIsNone(executor.requests[-1][1].prior_progress)

    def test_repeated_unchanged_actions_remain_executor_responsibility(self):
        coordinator, _, _ = self.make_coordinator(
            [{"action": "tap", "coords": [1, 1]}, {"action": "tap", "coords": [2, 2]},
             {"action": "tap", "coords": [2, 2]}, {"action": "tap", "coords": [2, 2]}],
            [execute_reply()])
        self.run_prefix(coordinator, 1)
        self.return_control(coordinator)
        self.assertEqual(coordinator.step(frame("execution_1")).kind, "action")
        coordinator.acknowledge_action(success=True)
        self.assertEqual(coordinator.step(frame("execution_2")).kind, "action")
        coordinator.acknowledge_action(success=True)
        result = coordinator.step(frame("execution_3"))
        self.assertEqual(result.kind, "action")
        self.assertEqual(result.action["action"], "tap")
        self.assertNotIn("stagnation_detected",
                         [event["event"] for event in coordinator.report()["events"]])

    def test_pause_forbids_observations_and_decisions(self):
        coordinator, planner, executor = self.make_coordinator([{"action": "done"}])
        coordinator.pause_for_handoff()
        with self.assertRaises(LifecycleError):
            coordinator.step(frame("private_during_handoff"))
        with self.assertRaises(LifecycleError):
            coordinator.pause_for_handoff()
        self.assertEqual(coordinator.decisions_used, 0)
        self.assertEqual(executor.requests, [])
        self.assertEqual(planner.requests, [])
        self.assertEqual(coordinator.report()["observations"], [])

    def test_return_requires_pause_and_fresh_frame_and_does_not_call_models(self):
        coordinator, planner, executor = self.make_coordinator(
            [{"action": "wait"}], [execute_reply()])
        with self.assertRaises(LifecycleError):
            coordinator.return_control(frame("too_early"))
        self.run_prefix(coordinator, 1)
        coordinator.pause_for_handoff()
        for invalid in (frame("prefix_0"), {"private_log": "resume_start"}):
            with self.subTest(invalid=type(invalid).__name__):
                with self.assertRaises(LifecycleError):
                    coordinator.return_control(invalid)
        coordinator.return_control(frame("returned"))
        with self.assertRaises(LifecycleError):
            coordinator.return_control(frame("duplicate_signal"))
        self.assertEqual(coordinator.phase, "preparing")
        self.assertEqual(planner.requests, [])
        self.assertEqual(len(executor.requests), 1)

    def test_direct_handoff_executes_locally_through_done_or_failure(self):
        for final_action, expected in (("done", "task_complete"), ("fail", "agent_failed")):
            with self.subTest(final_action=final_action):
                coordinator, planner, executor = self.make_coordinator(
                    [{"action": "wait"}, {"action": final_action}], [execute_reply()])
                returned = self.return_control(coordinator)
                first = coordinator.step(returned)
                self.assertEqual((first.kind, first.scope), ("action", "execution"))
                self.assertEqual(coordinator.cloud_calls, 1)
                coordinator.acknowledge_action(success=True)
                final = coordinator.step(frame("execution_end"))
                self.assertEqual(final.kind, expected)
                self.assertEqual(len(planner.requests), 1)
                self.assertEqual(len(executor.requests), 2)
                self.assertEqual(coordinator.report()["cloud_calls_after_handoff"], 0)
                self.assertIsNone(coordinator.report()["task_success"])

    def test_slim_execute_plan_is_expanded_for_local_execution(self):
        coordinator, _, executor = self.make_coordinator([{"action": "done"}], [slim_execute_reply()])
        result = coordinator.step(self.return_control(coordinator))
        self.assertEqual((result.kind, result.scope), ("task_complete", "execution"))
        instruction = executor.requests[0][1]
        self.assertEqual(instruction.scope, "execution")
        self.assertEqual(instruction.handoff.remaining_objective,
                         "Fill the requested email and save the same contact.")
        self.assertEqual(instruction.handoff.preserve, CONSTRAINTS)

    def test_completion_check_allows_recovery_done(self):
        action = {"action": "done", "reason": "verified",
                  "completion_check": self.completion_check()}
        coordinator, _, _ = self.make_coordinator(
            [action], [slim_execute_reply()], requires_completion_check=True)
        result = coordinator.step(self.return_control(coordinator))
        self.assertEqual(result.kind, "task_complete")
        self.assertEqual(coordinator.history[-1].action["completion_check"],
                         self.completion_check())

    def test_completion_check_rejects_missing_or_incomplete_done(self):
        cases = [
            {"action": "done", "reason": "missing"},
            {"action": "done", "reason": "wrong", "completion_check": []},
            {"action": "done", "reason": "wrong", "completion_check":
             self.completion_check(status="unverified")},
        ]
        for action in cases:
            with self.subTest(action=action):
                coordinator, _, _ = self.make_coordinator(
                    [action], [slim_execute_reply()], requires_completion_check=True)
                result = coordinator.step(self.return_control(coordinator))
                self.assertEqual((result.kind, result.error_category),
                                 ("agent_error", "completion_check_error"))
                self.assertTrue(coordinator.finished)

    def test_completion_check_requires_exact_ordered_coverage(self):
        two_condition_reply = PlanReply(SlimPlan(
            "EXECUTE", "Complete both visible requirements.",
            ("The title is saved.", "The unrelated note is unchanged."),
            CONSTRAINTS,
        ))
        valid = [
            {"condition_id": "c1", "status": "satisfied", "evidence": "Title is saved."},
            {"condition_id": "c2", "status": "satisfied", "evidence": "Note is unchanged."},
        ]
        invalid = [
            valid[:1],
            [valid[0], valid[0]],
            [valid[0], {"condition_id": "c3", "status": "satisfied", "evidence": "Note is unchanged."}],
            [valid[1], valid[0]],
        ]
        for check in invalid:
            with self.subTest(check=check):
                action = {"action": "done", "completion_check": check}
                coordinator, _, _ = self.make_coordinator(
                    [action], [two_condition_reply], requires_completion_check=True)
                result = coordinator.step(self.return_control(coordinator))
                self.assertEqual((result.kind, result.error_category),
                                 ("agent_error", "completion_check_error"))

        action = {"action": "done", "completion_check": valid}
        coordinator, _, _ = self.make_coordinator(
            [action], [two_condition_reply], requires_completion_check=True)
        result = coordinator.step(self.return_control(coordinator))
        self.assertEqual(result.kind, "task_complete")
        self.assertEqual(coordinator.history[-1].action["completion_check"], valid)

    def test_slim_verify_plan_is_expanded_for_local_verification(self):
        coordinator, _, executor = self.make_coordinator([{"action": "done"}], [slim_verify_reply()])
        result = coordinator.step(self.return_control(coordinator))
        self.assertEqual((result.kind, result.scope), ("verification_complete", "verification"))
        instruction = executor.requests[0][1]
        self.assertEqual(instruction.scope, "verification")
        self.assertEqual(instruction.verification.objective,
                         "Open the contact details and inspect the saved email.")

    def test_explicit_fallback_preserves_original_execution_and_never_replans(self):
        coordinator, planner, executor = self.make_coordinator(
            [{"action": "wait"}, {"action": "done"}], [fallback_reply()])
        returned = self.return_control(coordinator)
        self.assertEqual(coordinator.step(returned).scope, "fallback")
        self.assertTrue(executor.requests[0][1].resume_original)
        self.assertEqual(executor.requests[0][1].resume_reason,
                         "planner_could_not_form_reliable_guidance")
        coordinator.acknowledge_action(success=True)
        self.assertEqual(coordinator.step(frame("fallback_end")).kind, "task_complete")
        self.assertEqual(len(planner.requests), 1)
        self.assertTrue(coordinator.report()["fallback"]["resume_original_context"])
        for _, instruction in executor.requests:
            self.assertEqual(instruction.original_goal, GOAL)
            self.assertEqual(instruction.public_constraints, CONSTRAINTS)
            self.assertIsNone(instruction.handoff)

    def test_verification_completion_is_one_host_event_and_review_waits_for_next_step(self):
        coordinator, planner, executor = self.make_coordinator(
            [{"action": "wait"}, {"action": "done", "text": "Untrusted verbal claim"},
             {"action": "done"}], [verify_reply(), execute_reply()])
        self.run_prefix(coordinator, 1)
        returned = self.return_control(coordinator)
        result = coordinator.step(returned)
        self.assertEqual((result.kind, result.scope, result.decision_number),
                         ("verification_complete", "verification", 2))
        self.assertEqual(result.action["action"], "done")
        self.assertFalse(coordinator.finished)
        self.assertEqual(coordinator.phase, "reviewing")
        self.assertEqual(len(executor.requests), 2)
        self.assertEqual(len(planner.requests), 1)
        self.assertEqual(coordinator.remaining_decisions, 18)
        with self.assertRaises(LifecycleError):
            coordinator.acknowledge_action(success=True)
        result = coordinator.step(frame("review_current"))
        self.assertEqual((result.kind, result.scope, result.decision_number),
                         ("task_complete", "execution", 3))
        self.assertEqual(len(planner.requests), 2)
        self.assertEqual(len(executor.requests), 3)
        review = planner.requests[1]
        self.assertEqual(review.current_frame_id, "review_current")
        self.assertEqual(review.decisions_used, 2)
        self.assertEqual(review.decisions_remaining, 18)
        self.assertNotIn("Untrusted verbal claim", review.verification_report)
        self.assertEqual(review.history[-1].scope, "verification")
        self.assertIsNone(coordinator.report()["task_success"])

    def test_verification_failure_is_scoped_and_does_not_terminate_original_task(self):
        coordinator, planner, executor = self.make_coordinator(
            [{"action": "fail"}, {"action": "done"}], [verify_reply(), fallback_reply()])
        returned = self.return_control(coordinator)
        result = coordinator.step(returned)
        self.assertEqual((result.kind, result.scope), ("verification_failed", "verification"))
        self.assertFalse(coordinator.finished)
        self.assertEqual(len(planner.requests), 1)
        self.assertEqual(len(executor.requests), 1)
        self.assertEqual(coordinator.step(frame("review_failure")).scope, "fallback")
        self.assertTrue(executor.requests[-1][1].resume_original)
        self.assertEqual(coordinator.decisions_used, 2)
        self.assertEqual(len(planner.requests), 2)

    def test_second_verification_request_falls_back_without_third_cloud_call(self):
        coordinator, planner, executor = self.make_coordinator(
            [{"action": "done"}, {"action": "wait"}, {"action": "done"}],
            [verify_reply(), verify_reply()])
        returned = self.return_control(coordinator)
        self.assertEqual(coordinator.step(returned).kind, "verification_complete")
        result = coordinator.step(frame("second_analysis"))
        self.assertEqual((result.kind, result.scope), ("action", "fallback"))
        self.assertEqual(coordinator.verification_rounds_used, 1)
        self.assertEqual(coordinator.cloud_calls, 2)
        self.assertEqual(planner.requests[1].verification_rounds_remaining, 0)
        self.assertEqual(planner.requests[1].cloud_calls_remaining_after_this, 0)
        coordinator.acknowledge_action(success=True)
        self.assertEqual(coordinator.step(frame("fallback_done")).kind, "task_complete")
        self.assertEqual(len(planner.requests), 2)
        self.assertEqual(len(executor.requests), 3)
        self.assertEqual(coordinator.report()["cloud_calls_after_handoff"], 0)

    def test_original_goal_and_native_session_stay_stable_across_both_stages(self):
        coordinator, planner, executor = self.make_coordinator(
            [{"action": "wait"}, {"action": "done"}, {"action": "done"}],
            [verify_reply(), execute_reply()])
        self.run_prefix(coordinator, 1)
        coordinator.step(self.return_control(coordinator))
        coordinator.step(frame("last"))
        self.assertEqual(executor.starts, [("synthetic-contact", GOAL)])
        self.assertEqual([instruction.scope for _, instruction in executor.requests],
                         ["original", "verification", "execution"])
        for _, instruction in executor.requests:
            self.assertEqual(instruction.original_goal, GOAL)
            self.assertEqual(instruction.public_constraints, CONSTRAINTS)
        for request in planner.requests:
            self.assertEqual(request.original_goal, GOAL)
            self.assertEqual(request.public_constraints, CONSTRAINTS)
        self.assertEqual(coordinator.report()["original_goal"], GOAL)

    def test_api_error_consumes_one_cloud_call_and_omits_credentials_from_report(self):
        credential = "synthetic-secret-do-not-record"
        coordinator, planner, executor = self.make_coordinator(
            [{"action": "wait"}, {"action": "done"}],
            [TimeoutError(f"Authorization: Bearer {credential}; https://private.invalid/request")])
        self.assertEqual(coordinator.step(self.return_control(coordinator)).scope, "fallback")
        coordinator.acknowledge_action(success=True)
        coordinator.step(frame("fallback_final"))
        report = coordinator.report()
        serialized = json.dumps(report)
        self.assertNotIn(credential, serialized)
        self.assertNotIn("private.invalid", serialized)
        self.assertEqual(report["cloud_requests"][0]["error_type"], "TimeoutError")
        self.assertEqual(report["cloud_calls"], 1)
        self.assertEqual(len(planner.requests), 1)
        self.assertEqual(len(executor.requests), 2)

    def test_cloud_usage_records_numeric_costs_without_raw_response_fields(self):
        coordinator, _, _ = self.make_coordinator(
            [{"action": "done"}], [execute_reply(usage={
                "prompt_tokens": 12, "completion_tokens": 8, "api_key": "fake-secret",
                "raw_response": "private-body", "negative": -1, "bool": True,
            })])
        coordinator.step(self.return_control(coordinator))
        report = coordinator.report()
        self.assertEqual(report["cloud_requests"][0]["usage"],
                         {"prompt_tokens": 12, "completion_tokens": 8})
        self.assertNotIn("fake-secret", json.dumps(report))
        self.assertNotIn("private-body", json.dumps(report))

    def test_invalid_planner_reply_falls_back_without_retry_or_extra_local_decision(self):
        for invalid in ({"decision": "EXECUTE"}, PlanReply(Plan("EXECUTE", "Missing handoff"))):
            with self.subTest(invalid=type(invalid).__name__):
                coordinator, planner, executor = self.make_coordinator(
                    [{"action": "done"}], [invalid])
                result = coordinator.step(self.return_control(coordinator))
                self.assertEqual((result.kind, result.scope), ("task_complete", "fallback"))
                self.assertEqual(coordinator.decisions_used, 1)
                self.assertEqual(coordinator.cloud_calls, 1)
                self.assertEqual(len(planner.requests), 1)
                self.assertEqual(len(executor.requests), 1)

    def test_planner_validation_failure_records_sanitized_category(self):
        coordinator, planner, executor = self.make_coordinator(
            [{"action": "done"}], [PlanValidationError("EXECUTE cannot request verification")])
        coordinator.step(self.return_control(coordinator))
        record = coordinator.report()["cloud_requests"][0]
        self.assertEqual(record["error_type"], "PlanValidationError")
        self.assertEqual(record["error_category"], "execute_verification_conflict")
        self.assertNotIn("EXECUTE cannot request verification", json.dumps(record))

    def test_shared_twenty_decisions_include_prefix_verification_and_final_done_or_fail(self):
        for terminal, expected in (("done", "task_complete"), ("fail", "agent_failed")):
            with self.subTest(terminal=terminal):
                actions = [{"action": "wait"} for _ in range(18)]
                actions += [{"action": "done"}, {"action": terminal}]
                coordinator, planner, executor = self.make_coordinator(
                    actions, [verify_reply(), execute_reply()])
                self.run_prefix(coordinator, 18)
                verify = coordinator.step(self.return_control(coordinator))
                self.assertEqual(verify.decision_number, 19)
                self.assertEqual(verify.kind, "verification_complete")
                result = coordinator.step(frame("twentieth"))
                self.assertEqual(result.kind, expected)
                self.assertEqual(result.decision_number, TOTAL_DECISIONS)
                self.assertEqual(coordinator.remaining_decisions, 0)
                self.assertEqual(len(executor.requests), TOTAL_DECISIONS)
                self.assertEqual([item.decision_number for item in coordinator.history],
                                 list(range(1, 21)))
                self.assertEqual(planner.requests[0].decisions_used, 18)
                self.assertEqual(planner.requests[1].decisions_used, 19)
                self.assertEqual(coordinator.report()["task_success"], None)

    def test_runtime_budget_can_be_changed_without_changing_action_accounting(self):
        actions = [{"action": "wait"} for _ in range(29)] + [{"action": "done"}]
        coordinator, planner, executor = self.make_coordinator(
            actions, budget=RecoveryBudget(total_decisions=30))
        self.run_prefix(coordinator, 29)
        result = coordinator.step(frame("thirtieth"))
        self.assertEqual((result.kind, result.decision_number), ("task_complete", 30))
        self.assertEqual(coordinator.remaining_decisions, 0)
        self.assertEqual(len(executor.requests), 30)
        self.assertEqual(planner.requests, [])
        self.assertEqual(coordinator.report()["budget"]["total"], 30)
        semantics = coordinator.report()["budget_semantics"]
        self.assertEqual(semantics["version"], "shared_total_v1")
        self.assertEqual(semantics["planner_calls"],
                         "separate_preparation_counter_not_gui_decisions")
        self.assertTrue(semantics["verification"].startswith("verification_actions"))

    def test_executor_protocol_error_is_classified_separately_from_recovery_control(self):
        coordinator, planner, executor = self.make_coordinator(
            [RuntimeError("provider output")])
        result = coordinator.step(frame("protocol_error"))
        self.assertEqual(result.error_class, "gui_agent_error")
        self.assertEqual(coordinator.report()["error_class"], "gui_agent_error")

    def test_local_error_or_malformed_action_on_twentieth_decision_is_counted(self):
        for bad_action in (RuntimeError("private agent error body"), {"action": "unsupported"},
                           None, {"action": "tap", "value": float("nan")}):
            with self.subTest(bad_action=type(bad_action).__name__):
                coordinator, planner, executor = self.make_coordinator(
                    [{"action": "wait"} for _ in range(19)] + [bad_action])
                self.run_prefix(coordinator, 19)
                result = coordinator.step(frame("failed_decision"))
                self.assertEqual((result.kind, result.decision_number), ("agent_error", 20))
                self.assertEqual(coordinator.remaining_decisions, 0)
                self.assertEqual(len(executor.requests), 20)
                self.assertEqual(planner.requests, [])
                self.assertEqual(coordinator.history[-1].action["action"], "decision_error")
                self.assertNotIn("private agent error body", json.dumps(coordinator.report()))

    def test_twentieth_device_action_requires_ack_before_budget_exhaustion(self):
        coordinator, planner, executor = self.make_coordinator(
            [{"action": "wait"} for _ in range(20)])
        self.run_prefix(coordinator, 19)
        result = coordinator.step(frame("twentieth_action"))
        self.assertEqual((result.kind, result.decision_number), ("action", 20))
        self.assertFalse(coordinator.finished)
        with self.assertRaises(LifecycleError):
            coordinator.step(frame("unacknowledged_extra"))
        result = coordinator.acknowledge_action(success=True)
        self.assertEqual(result.kind, "budget_exhausted")
        self.assertTrue(coordinator.finished)
        with self.assertRaises(LifecycleError):
            coordinator.step(frame("twenty_first"))
        self.assertEqual(len(executor.requests), 20)
        self.assertEqual(planner.requests, [])

    def test_verification_finishing_on_twentieth_decision_ends_without_cloud_review(self):
        for terminal in ("done", "fail"):
            with self.subTest(terminal=terminal):
                coordinator, planner, executor = self.make_coordinator(
                    [{"action": "wait"} for _ in range(19)] + [{"action": terminal}],
                    [verify_reply()])
                self.run_prefix(coordinator, 18)
                result = coordinator.step(self.return_control(coordinator))
                self.assertEqual((result.kind, result.scope), ("action", "verification"))
                coordinator.acknowledge_action(success=True)
                result = coordinator.step(frame("verification_end"))
                self.assertEqual((result.kind, result.scope, result.decision_number),
                                 ("budget_exhausted", "verification", 20))
                self.assertEqual(result.action["action"], terminal)
                self.assertEqual(len(planner.requests), 1)
                self.assertEqual(len(executor.requests), 20)
                self.assertEqual(coordinator.history[-1].scope, "verification")

    def test_verification_cannot_start_with_only_one_decision_left(self):
        coordinator, planner, executor = self.make_coordinator(
            [{"action": "wait"} for _ in range(19)] + [{"action": "done"}],
            [verify_reply()])
        self.run_prefix(coordinator, 19)
        result = coordinator.step(self.return_control(coordinator))
        self.assertEqual((result.kind, result.scope, result.decision_number),
                         ("task_complete", "fallback", 20))
        self.assertEqual(coordinator.verification_rounds_used, 0)
        self.assertEqual(len(planner.requests), 1)
        self.assertEqual(len(executor.requests), 20)

    def test_device_action_requires_one_boolean_ack_and_cannot_pause_while_pending(self):
        coordinator, planner, executor = self.make_coordinator(
            [{"action": "tap", "x": 1, "y": 1}, {"action": "done"}])
        coordinator.step(frame("tap"))
        with self.assertRaises(LifecycleError):
            coordinator.step(frame("too_soon"))
        with self.assertRaises(LifecycleError):
            coordinator.pause_for_handoff()
        with self.assertRaises(TypeError):
            coordinator.acknowledge_action(success=1)
        self.assertEqual(coordinator.decisions_used, 1)
        self.assertEqual(len(executor.requests), 1)
        self.assertIsNone(coordinator.acknowledge_action(success=True))
        with self.assertRaises(LifecycleError):
            coordinator.acknowledge_action(success=True)
        self.assertEqual(coordinator.step(frame("after_ack")).kind, "task_complete")
        self.assertEqual(planner.requests, [])
        self.assertNotIn("too_soon", [item["evidence_id"]
                                     for item in coordinator.report()["observations"]])

    def test_device_error_is_separate_from_model_failure_or_success(self):
        coordinator, planner, executor = self.make_coordinator(
            [{"action": "tap", "x": 1, "y": 1}], [execute_reply()])
        coordinator.step(self.return_control(coordinator))
        result = coordinator.acknowledge_action(success=False)
        self.assertEqual((result.kind, result.scope, result.decision_number),
                         ("device_error", "execution", 1))
        report = coordinator.report()
        self.assertEqual(report["termination_reason"], "device_error")
        self.assertIsNone(report["task_success"])
        self.assertEqual(report["score_source"], "external_benchmark_required")
        self.assertEqual(len(planner.requests), 1)
        self.assertEqual(len(executor.requests), 1)
        with self.assertRaises(LifecycleError):
            coordinator.step(frame("after_device_error"))

    def test_device_error_on_last_decision_keeps_device_error_classification(self):
        coordinator, _, _ = self.make_coordinator([{"action": "wait"} for _ in range(20)])
        self.run_prefix(coordinator, 19)
        coordinator.step(frame("last_device_action"))
        self.assertEqual(coordinator.acknowledge_action(success=False).kind, "device_error")
        self.assertEqual(coordinator.decisions_used, 20)

    def test_frame_replay_and_evidence_id_reassignment_are_rejected(self):
        coordinator, planner, executor = self.make_coordinator(
            [{"action": "wait"}, {"action": "done"}], [execute_reply()])
        initial = frame("initial")
        coordinator.step(initial)
        coordinator.acknowledge_action(success=True)
        with self.assertRaises(LifecycleError):
            coordinator.step(initial)
        returned = self.return_control(coordinator)
        with self.assertRaises(LifecycleError):
            coordinator.step(frame(returned.evidence_id, color=(128, 0, 0)))
        self.assertEqual(coordinator.decisions_used, 1)
        self.assertEqual(planner.requests, [])
        self.assertEqual(len(executor.requests), 1)
        self.assertEqual(coordinator.step(returned).kind, "task_complete")

    def test_terminal_decisions_reject_further_steps_and_control_signals(self):
        coordinator, _, executor = self.make_coordinator([{"action": "done"}])
        coordinator.step(frame("end"))
        for operation in (
            lambda: coordinator.step(frame("after_end")),
            coordinator.pause_for_handoff,
            lambda: coordinator.return_control(frame("late_return")),
            lambda: coordinator.acknowledge_action(success=True),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(LifecycleError):
                    operation()
        self.assertEqual(coordinator.decisions_used, 1)
        self.assertEqual(len(executor.requests), 1)

    def test_untyped_observations_cannot_become_model_evidence(self):
        coordinator, planner, executor = self.make_coordinator([{"action": "done"}])
        for private_object in ({"screenshot": b"image", "scorer": "truth"}, object(), None):
            with self.subTest(value=type(private_object).__name__):
                with self.assertRaises(TypeError):
                    coordinator.step(private_object)
        self.assertEqual(coordinator.decisions_used, 0)
        self.assertEqual(coordinator.report()["observations"], [])
        self.assertEqual(planner.requests, [])
        self.assertEqual(executor.requests, [])

    def test_cloud_evidence_contains_only_typed_public_frames_and_own_history(self):
        coordinator, planner, _ = self.make_coordinator(
            [{"action": "wait"} for _ in range(4)] + [{"action": "done"}, {"action": "done"}],
            [verify_reply(), execute_reply()])
        self.run_prefix(coordinator, 1)
        returned = self.return_control(coordinator)
        coordinator.step(returned)
        coordinator.acknowledge_action(success=True)
        for evidence_id in ("verify_second", "verify_third"):
            coordinator.step(frame(evidence_id))
            coordinator.acknowledge_action(success=True)
        coordinator.step(frame("verify_done"))
        coordinator.step(frame("review_now"))
        first, review = planner.requests
        self.assertEqual([item.evidence_id for item in first.frames], ["prefix_0", "returned"])
        self.assertEqual(first.pre_handoff_frame_id, "prefix_0")
        self.assertEqual(first.current_frame_id, "returned")
        self.assertEqual([item.evidence_id for item in review.frames],
                         ["prefix_0", "returned", "verify_third", "verify_done", "review_now"])
        self.assertEqual(review.current_frame_id, "review_now")
        self.assertEqual(review.pre_handoff_frame_id, "prefix_0")
        for request in planner.requests:
            self.assertTrue(all(isinstance(item, PublicFrame) for item in request.frames))
            self.assertTrue(all(isinstance(item, OwnAction) for item in request.history))
        self.assertEqual([item.decision_number for item in review.history], list(range(1, 6)))
        self.assertEqual([item.scope for item in review.history],
                         ["original", "verification", "verification", "verification", "verification"])

    def test_history_results_and_report_are_defensive_copies(self):
        native_action = {"action": "tap", "x": 1, "y": 1, "metadata": {"tag": "original"}}
        coordinator, planner, _ = self.make_coordinator(
            [native_action, {"action": "done"}], [execute_reply()])
        result = coordinator.step(frame("initial"))
        native_action["metadata"]["tag"] = "executor mutation"
        result.action["metadata"]["tag"] = "host mutation"
        snapshot = coordinator.history
        snapshot[0].action["metadata"]["tag"] = "history mutation"
        report = coordinator.report()
        report["own_history"][0]["action"]["metadata"]["tag"] = "report mutation"
        report["budget"]["used"] = 99
        self.assertEqual(coordinator.history[0].action["metadata"]["tag"], "original")
        self.assertEqual(coordinator.report()["budget"]["used"], 1)
        coordinator.acknowledge_action(success=True)
        coordinator.step(self.return_control(coordinator))
        planner.requests[0].history[0].action["metadata"]["tag"] = "planner mutation"
        self.assertEqual(coordinator.history[0].action["metadata"]["tag"], "original")


class PublicEvidenceTypeTests(unittest.TestCase):
    def request(self):
        return PlanRequest(
            original_goal=GOAL, public_constraints=CONSTRAINTS,
            history=(OwnAction(1, {"action": "wait"}, "original", "pre"),),
            frames=(frame("pre"), frame("current")), current_frame_id="current",
            decisions_used=1, decisions_remaining=19,
            verification_rounds_remaining=1, cloud_calls_remaining_after_this=1,
            pre_handoff_frame_id="pre",
        )

    def test_requests_reject_untyped_frames_history_and_missing_evidence(self):
        request = self.request()
        invalid_fields = (
            {"frames": ({"private_observation": "truth"},)},
            {"history": ({"action": "wait", "private_event": "truth"},)},
            {"frames": (frame("pre"),)},
            {"frames": (frame("current"), frame("current")), "pre_handoff_frame_id": None},
            {"pre_handoff_frame_id": "not_provided"},
            {"history": (OwnAction(2, {"action": "wait"}, "original", "pre"),)},
            {"decisions_remaining": 20},
        )
        for fields in invalid_fields:
            with self.subTest(fields=tuple(fields)):
                with self.assertRaises(ValueError):
                    replace(request, **fields)

    def test_frames_validate_png_dimensions_and_reserved_evidence_ids(self):
        valid = frame("valid")
        for fields in ({"width": 3}, {"height": True}, {"png": b"not a png"},
                       {"evidence_id": "action_1"}, {"evidence_id": "../private"}):
            with self.subTest(fields=tuple(fields)):
                with self.assertRaises(ValueError):
                    replace(valid, **fields)
        metadata = valid.metadata()
        self.assertNotIn("png", metadata)
        self.assertEqual(len(metadata["sha256"]), 64)


if __name__ == "__main__":
    unittest.main()
