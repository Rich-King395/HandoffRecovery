"""Offline contract tests; no provider, benchmark, device, or private images."""

import base64
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from handoff_recovery.core.types import (
    OwnAction, PlanRequest, PublicFrame, SlimPlan, VerificationTask,
)
from handoff_recovery.planner import (
    OpenAICompatiblePlanner, PlannerConfig, PlannerError, PlanValidationError,
    build_messages, diagnose_plan_text, parse_plan, validate_plan,
)
from handoff_recovery.planner.client import _http, _NoRedirect


# Synthesized 1x1 transparent PNG, with no application or personal data.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def request(**changes):
    original = PlanRequest(
        original_goal="Set the sample contact's email to sample@example.test.",
        public_constraints=("Keep its existing phone number.",),
        history=(OwnAction(1, {"action": "click", "coordinate": [10, 20]}, "original", "pre_handoff"),),
        frames=(PublicFrame("pre_handoff", PNG, 1, 1, "Observed before action_1."),
                PublicFrame("after_return", PNG, 1, 1, "Fresh observation after return.")),
        current_frame_id="after_return", decisions_used=1, decisions_remaining=19,
        verification_rounds_remaining=1, cloud_calls_remaining_after_this=1,
        pre_handoff_frame_id="pre_handoff",
    )
    return replace(original, **changes)


def execute():
    return {
        "decision": "EXECUTE", "reason": "The supplied current image supports a complete remaining goal.",
        "facts": [{"claim": "The sample contact detail is visible.", "scope": "current", "evidence_ids": ["after_return"]}],
        "unknowns": [], "verification": None,
        "handoff": {"remaining_objective": "Set the sample contact's email to sample@example.test and keep its phone number.",
                    "preserve": ["Existing phone number."],
                    "completion_conditions": ["The saved contact shows sample@example.test and its original phone number."]},
    }


def verify():
    value = execute()
    value.update(
        decision="VERIFY", reason="The saved target fields are not visible.",
        unknowns=[{"question": "Which target fields are saved?", "blocks_handoff": True,
                   "reason": "This determines whether remaining editing is needed."}],
        verification={"objective": "Inspect the exact sample contact's saved fields.",
                      "required_evidence": ["Saved email and phone on the target detail view."],
                      "constraints": ["Do not edit, create, delete, or save task data."]},
        handoff=None,
    )
    return value


def envelope(plan=None, **changes):
    result = {
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(plan or execute())}}],
        "usage": {"prompt_tokens": 30, "completion_tokens": 40, "total_tokens": 70},
    }
    result.update(changes)
    return json.dumps(result).encode("utf-8")


class PlanValidationTests(unittest.TestCase):
    def test_slim_plan_branches_and_local_budget_validation(self):
        execute_value = {
            "decision": "EXECUTE", "objective": "Set the saved email.",
            "completion_conditions": ["The saved email is visible."],
            "constraints": ["Keep the phone number."],
        }
        verify_value = {
            "decision": "VERIFY", "objective": "Inspect the saved email.",
            "completion_conditions": ["The saved email is visible."],
            "constraints": ["Do not edit or save."],
        }
        for value in (execute_value, verify_value):
            plan = parse_plan(json.dumps(value), request())
            self.assertIsInstance(plan, SlimPlan)
            self.assertEqual(validate_plan(plan, request()), plan)
        with self.assertRaises(PlanValidationError):
            parse_plan(json.dumps(verify_value), request(verification_rounds_remaining=0))

    def test_slim_plan_rejects_legacy_fields_and_empty_execution_conditions(self):
        value = {"decision": "EXECUTE", "objective": "Do it.",
                 "completion_conditions": [], "constraints": []}
        with self.assertRaises(PlanValidationError):
            parse_plan(json.dumps(value), request())

    def test_slim_fallback_is_reserved_for_unreliable_guidance(self):
        value = {"decision": "FALLBACK", "objective": "Resume the original task from the current state.",
                 "completion_conditions": [], "constraints": []}
        plan = parse_plan(json.dumps(value), request())
        self.assertIsInstance(plan, SlimPlan)
        public = json.loads(build_messages(request())[1]["content"][0]["text"])
        self.assertIn("do not use FALLBACK for a completed task", public["branch_constraints"])
        value["completion_conditions"] = ["Done."]
        value["reason"] = "extra"
        with self.assertRaises(PlanValidationError):
            parse_plan(json.dumps(value), request())

    def test_valid_branches_and_dataclass_validation(self):
        fallback = dict(execute(), decision="FALLBACK", reason="Evidence remains incomplete.", handoff=None)
        for value in (execute(), verify(), fallback):
            with self.subTest(value["decision"]):
                plan = parse_plan(json.dumps(value), request())
                self.assertEqual(plan.decision, value["decision"])
                self.assertEqual(validate_plan(plan, request()), plan)

    def test_execute_supports_local_inspection_and_conditional_actions(self):
        value = execute()
        value["handoff"]["inspect_before_action"] = ["Open Appearance and read the explicit theme value."]
        value["handoff"]["conditional_actions"] = [{
            "when": "The theme is Light.",
            "then": "Select Dark and return to the weather page.",
        }]
        plan = parse_plan(json.dumps(value), request())
        self.assertEqual(plan.handoff.inspect_before_action,
                         ("Open Appearance and read the explicit theme value.",))
        self.assertEqual(plan.handoff.conditional_actions[0].when, "The theme is Light.")
        self.assertEqual(plan.handoff.conditional_actions[0].then,
                         "Select Dark and return to the weather page.")

    def test_rejects_json_wrappers_duplicates_and_nonfinite_constants(self):
        good = json.dumps(execute())
        bad = [f"```json\n{good}\n```", good + good, "[]", "null",
               good.replace('"decision": "EXECUTE"', '"decision": "FALLBACK", "decision": "EXECUTE"'),
               good.replace('"scope": "current"', '"scope": "current", "scope": "historical"'),
               good.replace('"unknowns": []', '"unknowns": NaN')]
        for value in bad:
            with self.subTest(value=value[:40]), self.assertRaises(PlanValidationError):
                parse_plan(value, request())

    def test_validation_failures_expose_only_bounded_categories(self):
        cases = [
            ("```json\n{}\n```", "invalid_json"),
            (json.dumps(dict(execute(), decision="UNKNOWN")), "decision_value"),
            (json.dumps(dict(execute(), handoff=None)), "handoff_type"),
            (json.dumps(execute()).replace('"decision": "EXECUTE"',
                                           '"decision": "EXECUTE", "decision": "EXECUTE"'),
             "duplicate_field"),
        ]
        for value, category in cases:
            with self.subTest(category=category):
                with self.assertRaises(PlanValidationError) as caught:
                    parse_plan(value, request())
                self.assertEqual(caught.exception.category, category)
                self.assertRegex(caught.exception.category, r"^[a-z_]+$")

    def test_rejects_incomplete_extra_and_wrongly_typed_fields(self):
        cases = [dict(execute(), decision="SUCCESS"), dict(execute(), extra="private"),
                 dict(execute(), reason=" "), dict(execute(), facts={}),
                 dict(execute(), handoff=None), dict(execute(), verification=verify()["verification"])]
        missing = execute()
        del missing["unknowns"]
        cases.append(missing)
        for value in cases:
            with self.subTest(value=value), self.assertRaises(PlanValidationError):
                parse_plan(json.dumps(value), request())

    def test_current_claim_needs_post_return_frame(self):
        for ids in (["action_1"], ["pre_handoff"], ["pre_handoff", "action_1"], ["private_scorer"], []):
            value = execute()
            value["facts"][0]["evidence_ids"] = ids
            with self.subTest(ids=ids), self.assertRaises(PlanValidationError):
                parse_plan(json.dumps(value), request())
        value["facts"][0].update(scope="historical", evidence_ids=["action_1"])
        self.assertEqual(parse_plan(json.dumps(value), request()).facts[0].scope, "historical")

    def test_multiple_verification_views_remain_available(self):
        req = request()
        req = replace(req, frames=req.frames + (PublicFrame("verification_1", PNG, 1, 1),),
                      current_frame_id="verification_1", verification_rounds_remaining=0,
                      cloud_calls_remaining_after_this=0)
        # A prior post-return view may still support a task field; semantic truth is not proven.
        self.assertEqual(parse_plan(json.dumps(execute()), req).facts[0].evidence_ids, ("after_return",))

    def test_execute_rejects_blocking_unknown_and_nonboolean_flag(self):
        for flag in (True, "false", 0):
            value = execute()
            value["unknowns"] = [dict(verify()["unknowns"][0], blocks_handoff=flag)]
            with self.subTest(flag=flag), self.assertRaises(PlanValidationError):
                parse_plan(json.dumps(value), request())

    def test_verify_obeys_all_three_budget_limits(self):
        for changes in ({"verification_rounds_remaining": 0}, {"cloud_calls_remaining_after_this": 0},
                        {"decisions_used": 19, "decisions_remaining": 1}):
            with self.subTest(changes=changes), self.assertRaises(PlanValidationError):
                parse_plan(json.dumps(verify()), request(**changes))
        plan = parse_plan(json.dumps(verify()), request(decisions_used=18, decisions_remaining=2))
        self.assertEqual(plan.decision, "VERIFY")

    def test_verify_requires_blocking_question_evidence_and_constraints(self):
        cases = [dict(verify(), unknowns=[]), dict(verify(), handoff=execute()["handoff"])]
        for name in ("required_evidence", "constraints"):
            value = verify()
            value["verification"][name] = []
            cases.append(value)
        for value in cases:
            with self.subTest(value=value), self.assertRaises(PlanValidationError):
                parse_plan(json.dumps(value), request())

    def test_fallback_cannot_supply_new_instructions(self):
        for value in (dict(execute(), decision="FALLBACK"), dict(verify(), decision="FALLBACK")):
            with self.assertRaises(PlanValidationError):
                parse_plan(json.dumps(value), request())


class PromptTests(unittest.TestCase):
    def test_sends_actual_images_labels_and_public_fields_only(self):
        req = request()
        object.__setattr__(req, "private_event", "DO_NOT_SEND_HIDDEN_SENTINEL")
        messages = build_messages(req)
        self.assertEqual([message["role"] for message in messages], ["system", "user"])
        content = messages[1]["content"]
        public = json.loads(content[0]["text"])
        self.assertEqual(public["original_task_verbatim"], req.original_goal)
        self.assertEqual(public["public_constraints"], list(req.public_constraints))
        self.assertEqual(public["own_action_history"][0]["evidence_id"], "action_1")
        self.assertIn("before action_1", public["images"][0]["observation_note"])
        self.assertEqual(public["pre_handoff_frame_id"], "pre_handoff")
        image_parts = [item for item in content if item["type"] == "image_url"]
        self.assertEqual(len(image_parts), 2)
        for item in image_parts:
            url = item["image_url"]["url"]
            self.assertTrue(url.startswith("data:image/png;base64,"))
            self.assertEqual(base64.b64decode(url.split(",", 1)[1]), PNG)
        self.assertNotIn("DO_NOT_SEND_HIDDEN_SENTINEL", json.dumps(messages))

    def test_verification_report_is_labeled_unverified_with_latest_frame(self):
        task = VerificationTask("Inspect saved fields.", ("Saved fields.",), ("Do not edit.",))
        req = request(verification=task, verification_report="The local agent says it is saved.",
                      verification_rounds_remaining=0, cloud_calls_remaining_after_this=0)
        req = replace(req, frames=req.frames + (PublicFrame("verification_1", PNG, 1, 1),),
                      current_frame_id="verification_1")
        public = json.loads(build_messages(req)[1]["content"][0]["text"])
        self.assertEqual(public["verification_update"]["executor_report_unverified"], req.verification_report)
        self.assertEqual([item["evidence_id"] for item in public["images"] if item["is_latest_observation"]], ["verification_1"])


class PlannerClientTests(unittest.TestCase):
    def setUp(self):
        self.config = PlannerConfig("https://example.test/custom/chat/completions", "test-vision-model", "SECRET_TEST_TOKEN")

    def test_explicit_configuration_ignores_environment(self):
        with patch.dict(os.environ, {"RECOVERY_CLOUD_API_KEY": "ENV_SECRET", "OPENAI_API_KEY": "OTHER_SECRET"}):
            with self.assertRaises(ValueError):
                PlannerConfig.from_mapping({"endpoint": self.config.endpoint, "model": self.config.model})
            config = PlannerConfig.from_mapping({"endpoint": self.config.endpoint, "model": self.config.model,
                                                 "api_key": "EXPLICIT_SECRET"})
        self.assertEqual(config.api_key, "EXPLICIT_SECRET")
        self.assertNotIn("EXPLICIT_SECRET", repr(config))

    def test_invalid_endpoints_and_limits_fail_without_disclosing_values(self):
        for endpoint in ("example.test/chat", "file:///tmp/secret", "https://example.test", "https://u:SECRET_TEST_TOKEN@example.test/chat",
                         "https://example.test/chat?key=SECRET_TEST_TOKEN", "https://example.test/chat#secret", "https://example.test:invalid/chat"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError) as caught:
                replace(self.config, endpoint=endpoint)
            self.assertNotIn("SECRET_TEST_TOKEN", str(caught.exception))
        for changes in ({"timeout_seconds": float("inf")}, {"timeout_seconds": 0},
                        {"max_tokens": True}, {"max_response_bytes": 0}, {"use_environment_proxy": "false"},
                        {"enable_thinking": "false"}, {"enable_thinking": 0}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(self.config, **changes)

    def test_one_call_preserves_endpoint_and_records_only_numeric_usage(self):
        transport = Mock(return_value=envelope(usage={"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7,
                                                     "provider_note": "SECRET_TEST_TOKEN", "api_key": "SECRET_TEST_TOKEN"}))
        planner = OpenAICompatiblePlanner(self.config, transport=transport)
        result = planner.plan(request())
        transport.assert_called_once()
        passed_config, payload = transport.call_args.args
        self.assertEqual(passed_config.endpoint, self.config.endpoint)
        self.assertEqual(payload["model"], self.config.model)
        self.assertEqual(payload["n"], 1)
        self.assertFalse(payload["stream"])
        response_format = payload["response_format"]
        self.assertEqual(response_format["type"], "json_schema")
        self.assertTrue(response_format["json_schema"]["strict"])
        schema = response_format["json_schema"]["schema"]
        self.assertEqual(schema["required"],
                         ["decision", "objective", "completion_conditions", "constraints"])
        self.assertFalse(schema["additionalProperties"])
        properties = schema["properties"]
        self.assertEqual(properties["objective"], {"type": "string"})
        self.assertEqual(properties["completion_conditions"],
                         {"type": "array", "items": {"type": "string"}})
        self.assertEqual(properties["constraints"],
                         {"type": "array", "items": {"type": "string"}})
        self.assertEqual(properties["use_prior_progress"], {"type": "boolean"})
        self.assertNotIn("SECRET_TEST_TOKEN", json.dumps(payload))
        self.assertEqual(result.usage, {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7})
        self.assertGreaterEqual(result.elapsed_seconds, 0)
        self.assertNotIn("SECRET_TEST_TOKEN", repr(planner.descriptor))
        self.assertTrue(planner.descriptor["use_environment_proxy"])

    def test_response_format_can_be_disabled_for_ab_testing(self):
        config = replace(self.config, response_format_mode="none")
        transport = Mock(return_value=envelope())
        OpenAICompatiblePlanner(config, transport=transport).plan(request())
        payload = transport.call_args.args[1]
        self.assertNotIn("response_format", payload)

    def test_response_format_mode_is_validated_and_mapped(self):
        with self.assertRaises(ValueError):
            replace(self.config, response_format_mode="xml")
        config = PlannerConfig.from_mapping({
            "endpoint": self.config.endpoint, "model": self.config.model,
            "api_key": self.config.api_key, "response_format_mode": "none",
        })
        self.assertEqual(config.response_format_mode, "none")

    def test_debug_capture_records_structure_and_raw_response(self):
        malformed = dict(execute(), extra="unexpected")
        with tempfile.TemporaryDirectory() as directory:
            config = replace(self.config, debug_response_dir=directory)
            transport = Mock(return_value=envelope(malformed))
            with self.assertRaises(PlanValidationError) as caught:
                OpenAICompatiblePlanner(config, transport=transport).plan(request())
            files = list(Path(directory).glob("planner_response_*.json"))
            self.assertEqual(len(files), 1)
            saved = json.loads(files[0].read_text())
            self.assertEqual(saved["error_category"], caught.exception.category)
            self.assertEqual(saved["diagnostic"]["root_extra"], ["extra"])
            self.assertEqual(saved["raw_response"], json.dumps(malformed))

    def test_plan_diagnostic_identifies_nested_shape(self):
        value = execute()
        value["handoff"]["conditional_actions"] = [{"when": "x", "then": "y", "extra": "z"}]
        diagnostic = diagnose_plan_text(json.dumps(value))
        self.assertEqual(diagnostic["nested_shape"]["location"], "handoff.conditional_actions[0]")

    def test_thinking_option_is_explicit_and_absent_by_default(self):
        for value in (None, False, True):
            config = PlannerConfig.from_mapping({
                "endpoint": self.config.endpoint, "model": self.config.model,
                "api_key": self.config.api_key, "enable_thinking": value,
            })
            transport = Mock(return_value=envelope())
            planner = OpenAICompatiblePlanner(config, transport=transport)
            with self.subTest(value=value):
                planner.plan(request())
                transport.assert_called_once()
                passed_config, payload = transport.call_args.args
                self.assertEqual(passed_config.endpoint, self.config.endpoint)
                if value is None:
                    self.assertNotIn("enable_thinking", payload)
                    self.assertNotIn("enable_thinking", planner.descriptor)
                else:
                    self.assertIs(payload["enable_thinking"], value)
                    self.assertIs(planner.descriptor["enable_thinking"], value)
        self.assertIsNone(self.config.enable_thinking)

    def test_http_status_survives_public_plan_call_without_error_text(self):
        for status in (401, 429, 503):
            error = HTTPError(self.config.endpoint, status, "SECRET_TEST_TOKEN", {},
                              io.BytesIO(b"UPSTREAM_PRIVATE_BODY"))
            opener = Mock()
            opener.open.side_effect = error
            with self.subTest(status=status), patch(
                    "handoff_recovery.planner.client.build_opener", return_value=opener):
                with self.assertRaises(PlannerError) as caught:
                    OpenAICompatiblePlanner(self.config).plan(request())
            opener.open.assert_called_once()
            self.assertEqual(caught.exception.http_status, status)
            self.assertIn(str(status), str(caught.exception))
            self.assertNotIn("SECRET_TEST_TOKEN", str(caught.exception))
            self.assertNotIn("UPSTREAM_PRIVATE_BODY", str(caught.exception))

    def test_transport_failure_has_no_retry_or_secret_in_error(self):
        for failure in (RuntimeError("SECRET_TEST_TOKEN"), PlannerError("SECRET_TEST_TOKEN")):
            transport = Mock(side_effect=failure)
            with self.subTest(failure=type(failure).__name__), self.assertRaises(PlannerError) as caught:
                OpenAICompatiblePlanner(self.config, transport=transport).plan(request())
            transport.assert_called_once()
            self.assertNotIn("SECRET_TEST_TOKEN", str(caught.exception))

    def test_rejects_oversized_invalid_truncated_and_tool_responses(self):
        truncated = {"choices": [{"finish_reason": "length", "message": {"content": json.dumps(execute())}}]}
        tool = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(execute()), "tool_calls": [{}]}}]}
        for raw in (b"SECRET_TEST_TOKEN", b"x" * (self.config.max_response_bytes + 1),
                    json.dumps(truncated).encode(), json.dumps(tool).encode(), envelope(choices=[])):
            transport = Mock(return_value=raw)
            with self.subTest(size=len(raw)), self.assertRaises(PlannerError) as caught:
                OpenAICompatiblePlanner(self.config, transport=transport).plan(request())
            transport.assert_called_once()
            self.assertNotIn("SECRET_TEST_TOKEN", str(caught.exception))

    def test_invalid_model_plan_is_not_retried(self):
        transport = Mock(return_value=envelope(dict(execute(), decision="SUCCESS")))
        with self.assertRaises(PlanValidationError):
            OpenAICompatiblePlanner(self.config, transport=transport).plan(request())
        transport.assert_called_once()

    def test_schema_valid_response_still_uses_local_plan_validation(self):
        malformed = dict(execute(), handoff=None)
        transport = Mock(return_value=envelope(malformed))
        with self.assertRaises(PlanValidationError) as caught:
            OpenAICompatiblePlanner(self.config, transport=transport).plan(request())
        self.assertEqual(caught.exception.category, "handoff_type")
        transport.assert_called_once()

    def test_default_transport_posts_exact_endpoint_and_caps_read(self):
        response = Mock()
        response.status = 200
        response.read.return_value = b"{}"
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        with patch("handoff_recovery.planner.client.build_opener", return_value=opener):
            self.assertEqual(_http(self.config, {"model": "test"}), b"{}")
        opener.open.assert_called_once()
        sent = opener.open.call_args.args[0]
        self.assertEqual(sent.full_url, self.config.endpoint)
        self.assertEqual(sent.get_method(), "POST")
        self.assertEqual(sent.get_header("Authorization"), "Bearer SECRET_TEST_TOKEN")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], self.config.timeout_seconds)
        response.read.assert_called_once_with(self.config.max_response_bytes + 1)

    def test_http_errors_and_redirects_never_reveal_response_body(self):
        error = HTTPError(self.config.endpoint, 401, "SECRET_TEST_TOKEN", {}, io.BytesIO(b"SECRET_TEST_TOKEN"))
        opener = Mock()
        opener.open.side_effect = error
        with patch("handoff_recovery.planner.client.build_opener", return_value=opener):
            with self.assertRaises(PlannerError) as caught:
                _http(self.config, {})
        self.assertIn("401", str(caught.exception))
        self.assertNotIn("SECRET_TEST_TOKEN", str(caught.exception))
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 307, "redirect", {}, "https://other.test"))


if __name__ == "__main__":
    unittest.main()
