"""Offline budget selection in the smoke and formal batch entry points."""
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch


def script(name):
    path = Path(__file__).resolve().parents[2] / "scripts" / (name + ".py")
    spec = importlib.util.spec_from_file_location("budget_" + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BudgetEntrypointTests(unittest.TestCase):
    def test_both_parsers_accept_positive_totals_above_old_ceilings(self):
        smoke, batch = script("run_live_smoke"), script("run_recovery_batch")
        smoke_args = ["--bench-root", "/unused", "--baseline", "/unused.json", "--device", "synthetic",
                      "--adb", "/unused-adb", "--appium-url", "http://appium.invalid", "--local-model", "synthetic",
                      "--local-base-url", "http://model.invalid/v1", "--mode", "no_takeover"]
        self.assertEqual(smoke.parse_args(smoke_args).max_decisions, 30)
        for module, base in ((smoke, smoke_args), (batch, ["--tasks", "3"])):
            for total in (1, 30, 250, 10000):
                with self.subTest(module=module.__name__, total=total):
                    self.assertEqual(module.parse_args(base + ["--max-decisions", str(total)]).max_decisions,
                                     total)
            for invalid in ("0", "-1", "20.5", "true"):
                with self.subTest(module=module.__name__, invalid=invalid), redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        module.parse_args(base + ["--max-decisions", invalid])

    def test_batch_records_default_environment_and_cli_override_without_execution(self):
        batch = script("run_recovery_batch")
        for env_total, cli_total, expected in ((None, None, 30), ("250", None, 250), ("250", 20, 20),
                                                ("0", None, None)):
            with self.subTest(env_total=env_total, cli_total=cli_total), tempfile.TemporaryDirectory() as directory:
                values = {"RECOVERY_PLANNER_PROVIDER": "synthetic", "RECOVERY_LOCAL_AGENT": "gui_owl",
                          "RECOVERY_PLANNER_SYNTHETIC_ENDPOINT": "http://planner.invalid/v1/chat/completions",
                          "RECOVERY_PLANNER_SYNTHETIC_MODEL": "synthetic", "RECOVERY_PLANNER_SYNTHETIC_API_KEY": "mock",
                          "GUI_OWL_BASE_URL": "http://model.invalid/v1", "GUI_OWL_MODEL": "synthetic"}
                if env_total is not None:
                    values["RECOVERY_MAX_DECISIONS"] = env_total
                summaries = []
                remove = shutil.rmtree

                def collect(path):
                    summaries.append(json.loads((path / "summary.json").read_text()))
                    batch.sys.stdout.streams[-1].close()
                    remove(path)

                args = ["--tasks", "3", "--dry-run", "--output-root", directory,
                        "--configuration-source", "explicit_executor_spec"]
                if cli_total is not None:
                    args += ["--max-decisions", str(cli_total)]
                with patch.object(batch, "_read_env", return_value=values), \
                     patch.object(batch, "_select_tasks", return_value=[(3, 2002)]), \
                     patch.object(batch, "_task_metadata", return_value={}), \
                     patch.object(batch, "_run_child", side_effect=AssertionError("No device/model subprocess")) as run, \
                     patch.object(batch.shutil, "rmtree", side_effect=collect), \
                     redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    if expected is None:
                        with self.assertRaisesRegex(ValueError, "positive integer"):
                            batch.main(args)
                    else:
                        self.assertEqual(batch.main(args), 0)
                        self.assertEqual(summaries[0]["max_decisions"], expected)
                    run.assert_not_called()
                self.assertFalse(list(Path(directory).iterdir()))
