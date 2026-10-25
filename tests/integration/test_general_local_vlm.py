"""Local general VLMs use the real Benchmark transport, parser and Runner offline."""
from collections import deque
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import replace
from importlib import import_module
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import shutil
import unittest
from unittest.mock import patch

from handoff_recovery.core.coordinator import RecoveryCoordinator
from handoff_recovery.core.types import Handoff, LocalInstruction, RecoveryBudget, VerificationTask
from handoff_recovery.executor import ExecutorSpec, create_executor
from handoff_recovery.integrations.handoffbench import HandoffRecoveryAgent, load_cloud_bindings
from tests.integration import test_native_execution_parity as runner_cases
from tests.integration.test_runner_bridge import Planner, execute_plan
from tests.unit.test_coordinator import frame


@unittest.skipUnless(os.environ.get("HANDOFFBENCH_ROOT"), "Set HANDOFFBENCH_ROOT for local VLM integration")
class GeneralLocalVLMTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(os.environ["HANDOFFBENCH_ROOT"]).resolve()
        self.requests = []

    def cases(self):
        for name, configuration_name in (("qwen35", "Qwen35Configuration"), ("lfm25_vl", "LFM25VLConfiguration")):
            bindings = load_cloud_bindings(self.root, name)
            cfg = getattr(bindings.api, configuration_name)(model="synthetic-" + name)
            bindings = replace(bindings, validate_configuration=lambda cfg=cfg: cfg)
            executor = create_executor(name, self.root, ExecutorSpec(name, "unused-spec-model"))
            executor._bindings = bindings
            yield name, bindings, cfg, executor

    @contextmanager
    def transport(self, contents):
        import httpx
        common = import_module("resumebench.models.cloud_common")
        pending = deque(contents)
        def handle(request):
            self.requests.append(json.loads(request.content))
            content = pending.popleft()
            if isinstance(content, Exception):
                raise content
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
                "role": "assistant", "content": content}}]})
        def client(_cfg):
            return httpx.AsyncClient(transport=httpx.MockTransport(handle), trust_env=False)
        with patch.object(common, "_http_client", side_effect=client), redirect_stdout(io.StringIO()):
            yield
        self.assertFalse(pending)

    def test_original_wire_matches_baseline_and_history_is_scoped(self):
        goal = "Save the requested setting."
        tap = '{"action":"tap","target":"own-action","coords":[1,1]}'
        for name, bindings, cfg, executor in self.cases():
            with self.subTest(model=name), tempfile.TemporaryDirectory() as directory:
                baseline = bindings.agent_class(); baseline._configuration = cfg
                visible = frame("original")
                image = Path(directory) / "screen.png"; image.write_bytes(visible.png)
                executor.start_task("synthetic", goal)
                original = LocalInstruction(goal, (), "original")
                verification = LocalInstruction(goal, (), "verification", completion_policy="native",
                    verification=VerificationTask("Read the visible value", ("Visible value",), ()))
                execution = LocalInstruction(goal, (), "execution", completion_policy="native",
                    handoff=Handoff("Finish saving", (), ("Saved setting",)),
                    last_action_feedback={"reason": "PRIVATE_FEEDBACK"})
                fallback = LocalInstruction(goal, (), "fallback", resume_original=True)
                start = len(self.requests)
                with self.transport([tap] * 7):
                    expected = baseline.decide({"screenshot": str(image), "width": visible.width,
                                                "height": visible.height}, goal, [])
                    self.assertEqual(executor.decide(visible, original), expected)
                    for index, instruction in enumerate((verification, verification, execution, execution, fallback)):
                        executor.decide(frame("scope-" + str(index)), instruction)
                requests = self.requests[start:]
                self.assertEqual(requests[0], requests[1])
                texts = [item["messages"][0]["content"][1]["text"] for item in requests]
                for index in (2, 4):
                    self.assertIn('oldest to newest): []', texts[index])
                for index in (3, 5, 6):
                    self.assertIn('"target": "own-action"', texts[index])
                self.assertIn("Read the visible value", texts[2])
                self.assertNotIn("Read the visible value", texts[4])
                self.assertIn("Finish saving", texts[4])
                self.assertNotIn("Finish saving", texts[6])
                self.assertNotIn("PRIVATE_FEEDBACK", str(texts))
                self.assertNotIn("Last action feedback", str(texts))
                self.assertEqual(executor.descriptor["model"], cfg.model)
                self.assertEqual(executor.descriptor["configuration_source"], "handoffbench_provider_validate_configuration")

    def test_invalid_decisions_and_last_budget_done_match_baseline_in_both_modes(self):
        for name, bindings, cfg, executor in self.cases():
            for mode in ("no_takeover", "takeover"):
                results = []
                for recovery in (False, True):
                    with self.subTest(model=name, mode=mode, recovery=recovery), tempfile.TemporaryDirectory() as directory:
                        planner = Planner([execute_plan()])
                        if recovery:
                            agent = HandoffRecoveryAgent(bench_path=self.root, planner=planner,
                                executor=executor, completion_policy="native", condition_updates_enabled=False)
                        else:
                            agent = bindings.agent_class(); agent._configuration = cfg
                        start = len(self.requests)
                        with self.transport(['not json', '{"action":"tap","coords":[9999,0]}',
                                             '{"action":"tap","coords":[28,56]}', '{"action":"done"}']):
                            result, device = runner_cases.NativeExecutionParityTests.run_agent(
                                self, agent, Path(directory) / "run", mode=mode, budget=4)
                        self.assertEqual(result["termination_reason"], "agent_done")
                        self.assertIs(result["task_success"], False)
                        self.assertEqual(sum(result["counts"]["decisions"].values()), 4)
                        self.assertEqual(len(self.requests) - start, 4)
                        self.assertEqual(len(device.public_actions), 1)
                        if recovery:
                            self.assertEqual(agent.coordinator.decisions_used, 4)
                            self.assertEqual(len(planner.calls), int(mode == "takeover"))
                            self.assertEqual(len(executor._context), 2)
                        results.append(result["termination_reason"])
                self.assertEqual(results[0], results[1])

    def test_invalid_outputs_exhaust_shared_budget_without_history_or_extra_planner_calls(self):
        for name, _bindings, _cfg, executor in self.cases():
            planner = Planner([execute_plan()])
            control = RecoveryCoordinator(task_id="synthetic", original_goal="Save setting", planner=planner,
                executor=executor, budget=RecoveryBudget(total_decisions=2), completion_policy="native")
            control.pause_for_handoff(); control.return_control(frame("returned"))
            start = len(self.requests)
            with self.subTest(model=name), self.transport(["not json"] * 2):
                self.assertEqual(control.step(frame("first")).kind, "invalid_decision")
                self.assertEqual(control.step(frame("second")).kind, "invalid_decision")
                self.assertEqual(control.step(frame("exhausted")).kind, "budget_exhausted")
            self.assertEqual((control.decisions_used, len(self.requests) - start, len(planner.calls)), (2, 2, 1))
            self.assertEqual(executor._context, [])

    def test_condition_switch_and_completion_policy_are_independent(self):
        claims = [{"condition_id": "c1", "status": "satisfied", "evidence": "Visible saved setting"}]
        for name, _bindings, _cfg, executor in self.cases():
            for policy, active in (("native", False), ("native", True), ("conditions", True)):
                with self.subTest(model=name, policy=policy, active=active):
                    control = RecoveryCoordinator(task_id="synthetic", original_goal="Save setting",
                        planner=Planner([execute_plan()]), executor=executor,
                        budget=RecoveryBudget(total_decisions=2), completion_policy=policy,
                        condition_updates_enabled=active)
                    control.pause_for_handoff(); control.return_control(frame("returned"))
                    action = {"action": "wait", **({"condition_updates": claims} if active else {})}
                    with self.transport([json.dumps(action), '{"action":"done"}']):
                        self.assertEqual(control.step(frame("first")).kind, "action")
                        self.assertEqual(control.report()["condition_status"][0]["status"], "pending")
                        control.acknowledge_action(success=True)
                        self.assertEqual(control.report()["condition_status"][0]["status"],
                                         "satisfied" if active else "pending")
                        self.assertEqual(control.step(frame("second")).kind, "task_complete")
                    prompt = self.requests[-1]["messages"][0]["content"][1]["text"]
                    self.assertNotIn("Visible saved setting", prompt)
                    self.assertEqual(control.decisions_used, 2)
                    self.assertIsNone(control.report()["task_success"])

    def test_request_error_is_fatal_classified_and_not_retried(self):
        import httpx
        for name, _bindings, _cfg, executor in self.cases():
            control = RecoveryCoordinator(task_id="synthetic", original_goal="Save setting",
                planner=Planner(), executor=executor, completion_policy="native")
            start = len(self.requests)
            with self.subTest(model=name), self.transport([httpx.ConnectError("private detail")]):
                result = control.step(frame("first"))
            self.assertEqual(result.error_category, "request_error")
            self.assertEqual((control.decisions_used, len(self.requests) - start), (1, 1))
            self.assertTrue(control.finished)
            self.assertNotIn("private detail", str(control.report()))

    def test_script_selection_and_dry_run_record_benchmark_configuration(self):
        scripts = Path(__file__).resolve().parents[2] / "scripts"
        modules = {}
        for name in ("run_live_smoke", "run_recovery_batch"):
            spec = importlib.util.spec_from_file_location("local_vlm_" + name, scripts / (name + ".py"))
            module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
            modules[name] = module
        smoke, batch = modules["run_live_smoke"], modules["run_recovery_batch"]
        base = ["--bench-root", str(self.root), "--baseline", "/unused.json", "--device", "synthetic",
                "--adb", "/unused-adb", "--appium-url", "http://appium.invalid", "--local-model", "synthetic",
                "--local-base-url", "http://model.invalid/v1",
                "--credentials-file", "/unused.env",
                "--planner-endpoint", "http://planner.invalid/v1/chat/completions", "--planner-model", "synthetic"]
        public_helpers = import_module("handoff_recovery.integrations.handoffbench")
        for name, bindings, cfg, _executor in self.cases():
            with self.subTest(model=name), tempfile.TemporaryDirectory() as directory:
                args = smoke.parse_args(base + ["--local-agent", name, "--completion-policy", "native",
                                               "--condition-updates", "disabled"])
                self.assertEqual(args.local_agent, name)
                values = {"RECOVERY_PLANNER_PROVIDER": "qwen",
                          "RECOVERY_PLANNER_QWEN_ENDPOINT": "http://planner.invalid/v1/chat/completions",
                          "RECOVERY_PLANNER_QWEN_MODEL": "synthetic-planner",
                          "RECOVERY_PLANNER_QWEN_API_KEY": "synthetic-credential"}
                summaries = []
                remove = shutil.rmtree
                def collect(path):
                    summaries.append(json.loads((path / "summary.json").read_text()))
                    batch.sys.stdout.streams[-1].close()
                    remove(path)
                with patch.object(batch, "_read_env", return_value=values), \
                        patch.object(batch, "_select_tasks", return_value=[(3, 2002)]), \
                        patch.object(batch, "_task_metadata", return_value={}), \
                        patch.object(batch, "_run_child", side_effect=AssertionError("No device/model subprocess")), \
                        patch.object(public_helpers, "load_cloud_bindings", return_value=bindings), \
                        patch.object(batch.shutil, "rmtree", side_effect=collect), \
                        redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertEqual(batch.main(["--bench-root", str(self.root), "--local-agent", name,
                        "--tasks", "3", "--dry-run", "--output-root", directory,
                        "--completion-policy", "native", "--condition-updates", "disabled"]), 0)
                executor = summaries[0]["executor"]
                self.assertEqual((executor["agent"], executor["model"], executor["base_url"]),
                                 (name, cfg.model, cfg.base_url))
                self.assertEqual(executor["max_output_tokens"], cfg.max_output_tokens)
                self.assertEqual(executor["condition_updates"], "disabled")
                self.assertEqual(executor["completion_policy"], "native")
                self.assertEqual(executor["recovery_history_mode"], "isolated")
                self.assertEqual(summaries[0]["max_decisions"], 30)


if __name__ == "__main__":
    unittest.main()
