"""Run the HandoffRecovery takeover experiment over selected HandoffBench tasks.

This entry point deliberately fixes the benchmark mode to takeover. It delegates
one task at a time to ``run_live_smoke.py`` so preparation, Runner, scoring and
environment restoration remain exactly the HandoffBench-backed implementation.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

import yaml


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from handoff_recovery.core.types import MAX_CONFIGURED_TOTAL_DECISIONS

MAX_CONFIGURED_DECISIONS = MAX_CONFIGURED_TOTAL_DECISIONS


class _Tee:
    """Mirror batch output to the terminal and the batch-local run.log."""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, value):
        for stream in self.streams:
            stream.write(value)
            stream.flush()
        return len(value)

    def flush(self):
        for stream in self.streams:
            stream.flush()

    def isatty(self):
        return any(stream.isatty() for stream in self.streams if hasattr(stream, "isatty"))


def _run_child(command: list[str]) -> int:
    """Stream one task subprocess into the batch tee without hiding progress."""
    process = subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True, bufsize=1)
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="")
    return process.wait()


def _read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _safe(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return value.strip("._-") or "unknown"


def _truth(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _path(value: str, *, base: Path = ROOT) -> str:
    path = Path(value).expanduser()
    return str(path if path.is_absolute() else (base / path).resolve())


def _planner(values: dict[str, str], provider: str) -> dict[str, str]:
    prefix = "RECOVERY_PLANNER_" + provider.upper() + "_"
    result = {
        "provider": provider,
        "endpoint": values.get(prefix + "ENDPOINT", ""),
        "api_key_var": prefix + "API_KEY",
        "model": values.get(prefix + "MODEL", ""),
        "disable_thinking": values.get(prefix + "DISABLE_THINKING", ""),
        "response_format": values.get(prefix + "RESPONSE_FORMAT", "json_schema"),
    }
    missing = [key for key in ("endpoint", "model") if not result[key]]
    if missing or not values.get(result["api_key_var"], ""):
        raise ValueError("Planner configuration is incomplete for provider " + provider)
    if result["response_format"] not in {"json_schema", "none"}:
        raise ValueError("Planner response format must be json_schema or none")
    return result


def _task_mapping(bench: Path) -> tuple[dict[int, int], set[int]]:
    document = json.loads((bench / "resumebench/configs/instance_ids.json").read_text())
    pairs = document["instances"]
    public_to_task = {int(row["instance_id"]): int(row["task_id"]) for row in pairs}
    retired_doc = json.loads((bench / "resumebench/configs/retired_instances.json").read_text())
    retired = {int(row["task_id"]) for row in retired_doc.get("instances", [])}
    return public_to_task, retired


def _task_metadata(bench: Path) -> dict[int, dict[str, object]]:
    """Load the frozen HandoffBench app and declared takeover type metadata."""
    tasks_doc = json.loads((bench / "resumebench/configs/tasks.json").read_text(encoding="utf-8"))
    tasks = tasks_doc.get("tasks", [])
    metadata = {
        int(task["id"]): {"app": task.get("app", "unknown"), "takeover_types": []}
        for task in tasks if isinstance(task, dict) and isinstance(task.get("id"), int)
    }
    recipes_doc = yaml.safe_load((bench / "resumebench/configs/recipes.yaml").read_text(encoding="utf-8"))
    for recipe in (recipes_doc or {}).get("recipes", []):
        if not isinstance(recipe, dict) or not isinstance(recipe.get("task_id"), int):
            continue
        types = []
        for scenario in recipe.get("scenarios", []) or []:
            if isinstance(scenario, dict):
                types.extend(str(value) for value in scenario.get("takeover_type", []) or [])
        if recipe["task_id"] in metadata:
            metadata[recipe["task_id"]]["takeover_types"] = list(dict.fromkeys(types))
    return metadata


def _select_tasks(args: argparse.Namespace, bench: Path) -> list[tuple[int, int]]:
    public_to_task, retired = _task_mapping(bench)
    task_to_public = {task: public for public, task in public_to_task.items()}
    if args.all_tasks:
        selected = [(public, task) for public, task in sorted(public_to_task.items())
                    if task not in retired]
    else:
        selected = []
        for task in args.tasks:
            if task not in task_to_public:
                raise ValueError(f"Unknown or retired stable task_id: {task}")
            if task in retired:
                raise ValueError(f"Task is retired: {task}")
            selected.append((task_to_public[task], task))
    if not selected:
        raise ValueError("No active tasks selected")
    return selected


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--tasks", type=int, nargs="+", help="Stable HandoffBench task IDs")
    selection.add_argument("--all", dest="all_tasks", action="store_true",
                           help="Run every active HandoffBench task")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--bench-root", type=Path)
    parser.add_argument("--output-root", type=Path, default=ROOT / "results")
    parser.add_argument("--device")
    parser.add_argument("--adb", type=Path)
    parser.add_argument("--appium-url")
    parser.add_argument("--local-base-url",
                        help="Override the configured local agent OpenAI-compatible base URL")
    parser.add_argument("--local-agent", choices=("phonebuddy", "gui_owl", "qwen", "deepseek", "openai", "claude", "ui_voyager", "holo"),
                        help="GUI Agent adapter; defaults to RECOVERY_LOCAL_AGENT")
    parser.add_argument("--local-timeout-seconds", type=float)
    parser.add_argument("--local-total-timeout-seconds", type=float)
    parser.add_argument("--local-proxy-mode")
    parser.add_argument("--local-max-output-tokens", type=int)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--on-task-error", choices=("continue", "stop"), default="continue")
    parser.add_argument("--retention", choices=("detailed", "compact", "summary"))
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--planner-response-format", choices=("json_schema", "none"))
    parser.add_argument("--planner-adapter",
                        help="Planner registry name or module:factory path")
    parser.add_argument("--planner-model",
                        help="Override the configured Planner model for this batch")
    parser.add_argument("--max-decisions", type=int,
                        help="Shared local GUI decision budget for every trial (default 20, maximum 200)")
    parser.add_argument("--planner-debug-response-dir", type=Path)
    parser.add_argument("--disable-structured-progress", action="store_true",
                        help="A/B control: omit the compact pre-handoff progress summary")
    parser.add_argument("--disable-completion-check", action="store_true",
                        help="A/B control: omit and do not enforce recovery completion_check")
    parser.add_argument("--executor-adapter",
                        help="GUI Agent registry name or module:factory path; defaults to --local-agent")
    args = parser.parse_args(argv)
    if args.repetitions < 1:
        parser.error("--repetitions must be positive")
    if args.max_decisions is not None and not 1 <= args.max_decisions <= MAX_CONFIGURED_DECISIONS:
        parser.error(f"--max-decisions must be an integer from 1 to {MAX_CONFIGURED_DECISIONS}")
    for value, name in ((args.local_timeout_seconds, "--local-timeout-seconds"),
                        (args.local_total_timeout_seconds, "--local-total-timeout-seconds")):
        if value is not None and value <= 0:
            parser.error(f"{name} must be positive")
    if args.local_max_output_tokens is not None and args.local_max_output_tokens < 1:
        parser.error("--local-max-output-tokens must be positive")
    return args


def _compact(case: Path) -> None:
    """Remove bulky lifecycle evidence while retaining task diagnostics."""
    for name in ("preparation", "restore"):
        path = case / name
        if path.exists():
            shutil.rmtree(path)


def _summary_only(case: Path) -> None:
    """Delete the per-task artifact after its result is folded into the batch."""
    if case.exists():
        shutil.rmtree(case)


def _compact_succeeded(case: Path) -> bool:
    """Return whether the child durably transitioned its environment journal.

    A task directory must not be removed while the journal still points at its
    full result seal. The marker is written by the child only after
    ``compact_result`` succeeds.
    """
    return any(case.glob("*/environment/compact-result-receipt.json"))


def _write_report(path: Path, summary: dict) -> None:
    trials = summary["trials"]
    valid = [row for row in trials if row.get("takeover_status") == "VALID"]
    successes = sum(row.get("recovery_success") is True for row in valid)
    task_successes = sum(row.get("task_success") is True for row in valid)
    collected = [row for row in trials if row.get("status") == "collected"]
    triggered = [row for row in trials if row.get("takeover_status") not in (None, "NOT_TRIGGERED")]
    not_triggered = sum(row.get("takeover_status") == "NOT_TRIGGERED" for row in trials)
    flow_passed = sum(row.get("flow_passed") is True for row in collected)
    task_successes_all = sum(row.get("task_success") is True for row in trials)
    task_failures_all = sum(row.get("task_success") is False for row in trials)
    error_trials = [row for row in trials if row.get("status") == "error"]
    cloud_calls = [row.get("cloud_calls") for row in valid if isinstance(row.get("cloud_calls"), int)]
    decisions = {}
    branch_counts = {"EXECUTE": 0, "VERIFY": 0, "FALLBACK": 0}
    termination_reasons = {}
    error_types = {}
    error_classes = {}
    budget_values = []
    budget_totals = []
    for row in valid:
        decision = row.get("planner_decision")
        if decision:
            decisions[decision] = decisions.get(decision, 0) + 1
            if decision in branch_counts:
                branch_counts[decision] += 1
        reason = row.get("termination_reason")
        if reason:
            termination_reasons[reason] = termination_reasons.get(reason, 0) + 1
        error_class = row.get("error_class")
        if error_class:
            error_classes[error_class] = error_classes.get(error_class, 0) + 1
        for error_type in row.get("cloud_error_types", []) or []:
            error_types[error_type] = error_types.get(error_type, 0) + 1
        if isinstance(row.get("budget_used"), int):
            budget_values.append(row["budget_used"])
        if isinstance(row.get("budget_total"), int):
            budget_totals.append(row["budget_total"])
    budget_exhausted = sum(
        isinstance(row.get("budget_used"), int)
        and isinstance(row.get("budget_total"), int)
        and row["budget_used"] >= row["budget_total"]
        for row in valid
    )
    def pct(n: int, d: int) -> str:
        return f"{(100 * n / d):.1f}% ({n}/{d})" if d else "n/a (0/0)"

    by_type = {}
    by_app = {}
    for row in trials:
        types = row.get("takeover_types") or ["unspecified"]
        for takeover_type in types:
            group = by_type.setdefault(str(takeover_type), [])
            group.append(row)
        app = str(row.get("app") or "unknown")
        by_app.setdefault(app, []).append(row)

    type_lines = [
        "| Takeover type | Planned | Valid | Task success | Recovery success |",
        "|---|---:|---:|---:|---:|",
    ]
    for takeover_type, rows in sorted(by_type.items()):
        valid_rows = [row for row in rows if row.get("takeover_status") == "VALID"]
        type_lines.append(
            f"| `{takeover_type}` | {len(rows)} | {len(valid_rows)} | "
            f"{pct(sum(row.get('task_success') is True for row in valid_rows), len(valid_rows))} | "
            f"{pct(sum(row.get('recovery_success') is True for row in valid_rows), len(valid_rows))} |"
        )
    app_lines = [
        "| App | Trials | Task success | Valid takeover recovery success |",
        "|---|---:|---:|---:|",
    ]
    for app, rows in sorted(by_app.items()):
        valid_rows = [row for row in rows if row.get("takeover_status") == "VALID"]
        app_lines.append(
            f"| `{app}` | {len(rows)} | "
            f"{pct(sum(row.get('task_success') is True for row in rows), len(rows))} | "
            f"{pct(sum(row.get('recovery_success') is True for row in valid_rows), len(valid_rows))} |"
        )

    lines = [
        "# HandoffRecovery takeover batch",
        "",
        f"Planner: `{summary['planner']['provider']}` / `{summary['planner']['model']}` "
        f"(adapter `{summary['planner'].get('adapter', 'unknown')}`)",
        f"Local agent: `{summary['executor']['agent']}` / `{summary['executor']['model']}` "
        f"(adapter `{summary['executor'].get('adapter', 'unknown')}`)",
        f"Completion check: `{summary['executor'].get('completion_check_enabled', False)}`",
        f"Decision budget: `{summary.get('max_decisions', 'unknown')}`",
        "Budget semantics: one shared GUI decision budget across original/prefix/resume/recovery; "
        "Planner calls and takeover actions are separate counters; VERIFY actions consume GUI budget.",
        f"Selected tasks: {summary['selected_task_count']}; repetitions: {summary['repetitions']}",
        "",
        "| Metric | Result |",
        "|---|---:|",
        f"| Collected trials | {len(trials)} |",
        f"| Valid takeovers | {pct(len(valid), len(trials))} |",
        f"| Takeover trigger rate | {pct(len(triggered), len(trials))} |",
        f"| Not triggered | {not_triggered}/{len(trials)} |",
        f"| Flow passed | {pct(flow_passed, len(collected))} |",
        f"| Task success (all collected trials) | {pct(task_successes_all, len(trials))} |",
        f"| Task failures (all collected trials) | {task_failures_all}/{len(trials)} |",
        f"| Task success (valid takeovers) | {pct(task_successes, len(valid))} |",
        f"| Recovery success (valid takeovers) | {pct(successes, len(valid))} |",
        f"| Recovery coverage (all planned trials) | {pct(successes, len(trials))} |",
        f"| Budget exhausted (valid takeovers) | {budget_exhausted}/{len(valid)} |",
        f"| Planner calls (valid takeovers) | {sum(cloud_calls) if cloud_calls else 0} total; "
        f"{(sum(cloud_calls) / len(cloud_calls)):.2f} average |" if cloud_calls
        else "| Planner calls (valid takeovers) | 0 total; n/a average |",
        "",
        "## Recovery-method diagnostics",
        "",
        "These metrics evaluate the recovery controller independently of the external task score.",
        "",
        "| Metric | Result |",
        "|---|---:|",
        f"| Planner decisions | {', '.join(f'{k}={v}' for k, v in sorted(decisions.items())) or 'none recorded'} |",
        f"| Recovery branches | EXECUTE={branch_counts['EXECUTE']}; VERIFY={branch_counts['VERIFY']}; FALLBACK={branch_counts['FALLBACK']} |",
        f"| Fallback trials | {sum(bool(row.get('fallback')) for row in valid)} |",
        f"| Cloud calls after handoff | {sum(row.get('cloud_calls_after_handoff', 0) for row in valid)} |",
        f"| Verification rounds | {sum(row.get('verification_rounds', 0) for row in valid)} |",
        f"| Planner/request errors | {sum(bool(row.get('cloud_error_types')) for row in trials)} trials |",
        f"| Error trials | {len(error_trials)}/{len(trials)} |",
        f"| Termination reasons | {', '.join(f'{k}={v}' for k, v in sorted(termination_reasons.items())) or 'none recorded'} |",
        f"| Error types | {', '.join(f'{k}={v}' for k, v in sorted(error_types.items())) or 'none recorded'} |",
        f"| Error classes | {', '.join(f'{k}={v}' for k, v in sorted(error_classes.items())) or 'none recorded'} |",
        f"| Decision budget used | {sum(budget_values)}/{sum(budget_totals)} total; "
        f"{(sum(budget_values) / len(budget_values)):.2f} average |" if budget_values and budget_totals
        else "| Decision budget used | n/a |",
        "",
        "## Recovery success by takeover type",
        "",
        "Declared scenario labels come from HandoffBench recipes; multi-label rows are included in each declared group and must not be summed.",
        "",
        *type_lines,
        "",
        "## Task success by app",
        "",
        *app_lines,
        "",
        ("Detailed evidence is retained per task while `detailed` retention is enabled."
         if summary.get("retention") == "detailed" else
         "Per-task directories are removed after aggregation; only this batch summary and report are retained."),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    env_path = args.env_file.resolve()
    values = _read_env(env_path)
    bench = (args.bench_root or Path(values.get("HANDOFFBENCH_ROOT", "../ResumeBench/HandoffBench"))).resolve()
    provider = values.get("RECOVERY_PLANNER_PROVIDER", "").strip().lower()
    planner = _planner(values, provider)
    planner_adapter = (args.planner_adapter or values.get("RECOVERY_PLANNER_ADAPTER", "openai_compatible").strip())
    if not planner_adapter:
        planner_adapter = "openai_compatible"
    if args.planner_response_format:
        planner["response_format"] = args.planner_response_format
    if args.planner_model:
        planner["model"] = args.planner_model
    agent_name = (args.local_agent or values.get("RECOVERY_LOCAL_AGENT", "")).strip().lower()
    prefixes = {"phonebuddy": "PHONEBUDDY_", "gui_owl": "GUI_OWL_",
                "qwen": "QWEN_", "deepseek": "DEEPSEEK_",
                "openai": "OPENAI_", "claude": "CLAUDE_",
                "ui_voyager": "UI_VOYAGER_", "holo": "HOLO_"}
    if agent_name not in prefixes:
        raise ValueError("RECOVERY_LOCAL_AGENT must be one of phonebuddy, gui_owl, qwen, deepseek, openai, claude, ui_voyager or holo")
    local_prefix = prefixes[agent_name]
    local = {
        "agent": agent_name,
        "base_url": values.get(local_prefix + "BASE_URL", ""),
        "model": values.get(local_prefix + "MODEL", ""),
        # Keep credentials separate from model settings while allowing an
        # explicit alias when Planner and GUI Agent share one provider key.
        "key_var": values.get(
            local_prefix + "API_KEY_VAR",
            {
                # HandoffBench uses the provider's native credential name for
                # Claude; keep an explicit override available for relays.
                "qwen": "DASHSCOPE_API_KEY",
                "claude": "ANTHROPIC_API_KEY",
            }.get(agent_name, local_prefix + "API_KEY"),
        ).strip(),
        "prompt_path": _path(values[local_prefix + "PROMPT_PATH"]) if values.get(local_prefix + "PROMPT_PATH") else "",
    }
    local["timeout_seconds"] = args.local_timeout_seconds or float(values.get(local_prefix + "TIMEOUT_SECONDS", "120"))
    local["total_timeout_seconds"] = args.local_total_timeout_seconds or float(values.get(local_prefix + "TOTAL_TIMEOUT_SECONDS", "120"))
    local["proxy_mode"] = args.local_proxy_mode or values.get(local_prefix + "PROXY_MODE", "environment")
    local["max_output_tokens"] = args.local_max_output_tokens or int(values.get(local_prefix + "MAX_OUTPUT_TOKENS", "4096"))
    if args.local_base_url:
        local["base_url"] = args.local_base_url
    if agent_name not in {"qwen", "deepseek", "openai", "claude"} and (
            (agent_name != "qwen" and not local["base_url"]) or not local["model"]):
        raise ValueError("Local agent configuration is incomplete for " + agent_name)
    if agent_name in {"qwen", "deepseek", "openai", "claude"}:
        # Cloud GUI settings are owned by the selected HandoffBench provider.
        # Resolve them before naming the run so result metadata and folder names
        # cannot silently describe a different model than the request used.
        try:
            from handoff_recovery.integrations.handoffbench import load_cloud_bindings
            bindings = load_cloud_bindings(bench, agent_name)
            bench_configuration = bindings.validate_configuration()
        except Exception as exc:
            raise ValueError(
                f"Could not load {agent_name} configuration from HandoffBench: {type(exc).__name__}"
            ) from None
        local["model"] = bench_configuration.model
        local["base_url"] = bench_configuration.base_url
        local["configuration_source"] = "handoffbench_provider_validate_configuration"
    executor_adapter = (args.executor_adapter
                        or values.get("RECOVERY_EXECUTOR_ADAPTER", agent_name).strip()
                        or agent_name)
    max_decisions = args.max_decisions or int(values.get("RECOVERY_MAX_DECISIONS", "20"))
    if not 1 <= max_decisions <= MAX_CONFIGURED_DECISIONS:
        raise ValueError(
            f"RECOVERY_MAX_DECISIONS must be an integer from 1 to {MAX_CONFIGURED_DECISIONS}"
        )
    selected = _select_tasks(args, bench)
    task_metadata = _task_metadata(bench)
    retention = args.retention or values.get("RECOVERY_RESULT_RETENTION", "detailed")
    if retention not in {"detailed", "compact", "summary"}:
        raise ValueError("RECOVERY_RESULT_RETENTION must be detailed, compact or summary")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid.uuid4().hex[:8]
    output = (args.output_root /
              f"{_safe(planner['model'])}_{_safe(local['model'])}_"
              f"{len(selected) * args.repetitions}_{max_decisions}_{run_id}").resolve()
    output.mkdir(parents=True, exist_ok=False)
    log_stream = (output / "run.log").open("w", encoding="utf-8", buffering=1)
    sys.stdout = _Tee(sys.stdout, log_stream)
    sys.stderr = _Tee(sys.stderr, log_stream)
    plan = [(public, task) for public, task in selected for _ in range(args.repetitions)]
    summary = {
        "scope": "handoff_recovery_formal_takeover_batch",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "mode": "takeover",
        "selected_task_count": len(selected),
        "repetitions": args.repetitions,
        "retention": retention,
        "planner": {"provider": planner["provider"], "model": planner["model"],
                     "adapter": planner_adapter},
        "executor": {"agent": local["agent"], "adapter": executor_adapter,
                      "model": local["model"], "base_url": local["base_url"],
                      "configuration_source": local.get("configuration_source", "recovery_env"),
                      "completion_check_enabled": bool(
                          local["agent"] == "phonebuddy" and not args.disable_completion_check)},
        "max_decisions": max_decisions,
        "trials": [],
    }
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    if args.dry_run:
        print(json.dumps({"output": str(output), "tasks": [task for _, task in plan]}, indent=2))
        shutil.rmtree(output)
        return 0

    baseline = _path(str(args.baseline)) if args.baseline else (
        _path(values["RECOVERY_ENVIRONMENT_BASELINE"])
        if values.get("RECOVERY_ENVIRONMENT_BASELINE") else "")
    device = args.device or values.get("RECOVERY_DEVICE_SERIAL", "")
    adb = _path(str(args.adb)) if args.adb else (
        _path(values["RECOVERY_ADB_PATH"]) if values.get("RECOVERY_ADB_PATH") else "")
    appium = args.appium_url or values.get("RECOVERY_APPIUM_URL", "")
    if not all((baseline, device, adb, appium)):
        raise ValueError("Device, Appium, ADB and baseline settings are required")
    configured_python = values.get("RECOVERY_PYTHON", "").strip()
    benchmark_python = bench / ".runtime/appium-venv/bin/python"
    python = configured_python or (str(benchmark_python) if benchmark_python.is_file() else sys.executable)
    smoke = Path(__file__).with_name("run_live_smoke.py")
    for index, (public, task_id) in enumerate(plan, 1):
        case = output / f"task_{task_id}_trial_{index}"
        command = [python, str(smoke), "--bench-root", str(bench), "--baseline", baseline,
                   "--device", device, "--adb", adb, "--appium-url", appium,
                   "--instances", str(public), "--mode", "takeover",
                   "--credentials-file", str(env_path), "--planner-key-var", planner["api_key_var"],
                   "--planner-endpoint", planner["endpoint"], "--planner-model", planner["model"],
                   "--planner-adapter", planner_adapter,
                   "--planner-response-format", planner["response_format"],
                   "--max-decisions", str(max_decisions),
                   "--local-agent", local["agent"], "--local-base-url", local["base_url"],
                   "--local-model", local["model"], "--local-key-var", local["key_var"],
                   "--executor-adapter", executor_adapter,
                   "--local-timeout-seconds", str(local["timeout_seconds"]),
                   "--local-total-timeout-seconds", str(local["total_timeout_seconds"]),
                   "--local-proxy-mode", local["proxy_mode"],
                   "--local-max-output-tokens", str(local["max_output_tokens"]),
                   "--output", str(case)]
        if args.disable_structured_progress:
            command.append("--disable-structured-progress")
        if args.disable_completion_check:
            command.append("--disable-completion-check")
        if args.planner_debug_response_dir:
            debug_dir = args.planner_debug_response_dir.resolve() / f"task_{task_id}_trial_{index}"
            command.extend(("--planner-debug-response-dir", str(debug_dir)))
        if _truth(planner["disable_thinking"]):
            command.append("--planner-disable-thinking")
        if retention == "summary":
            # The task directory is removed after the child exits. Compact the
            # sealed result first so the host journal no longer references the
            # deleted result-seal.json evidence.
            command.append("--compact-environment-result")
        if local["prompt_path"]:
            command.extend(("--local-prompt-path", local["prompt_path"]))
        print(f"[{index}/{len(plan)}] task {task_id} (public instance {public})", flush=True)
        completed_returncode = _run_child(command)
        child_summary_path = case / "summary.json"
        if child_summary_path.exists():
            child = json.loads(child_summary_path.read_text())
            for trial in child.get("trials", []):
                result = trial.get("benchmark_result", {})
                directory = trial.get("directory")
                recovery = {}
                if directory:
                    recovery_path = Path(directory) / "recovery.json"
                    if recovery_path.exists():
                        try:
                            recovery = json.loads(recovery_path.read_text(encoding="utf-8"))
                        except (OSError, json.JSONDecodeError):
                            recovery = {}
                budget = recovery.get("budget", {})
                requests = recovery.get("cloud_requests", [])
                plans = [item.get("plan", {}) for item in requests if isinstance(item, dict)]
                summary["trials"].append({
                    "task_id": task_id, "instance_id": public,
                    "app": task_metadata.get(task_id, {}).get("app", "unknown"),
                    "takeover_types": task_metadata.get(task_id, {}).get("takeover_types", []),
                    "status": trial.get("status"), "task_success": result.get("task_success"),
                    "recovery_success": result.get("recovery_success"),
                    "takeover_status": result.get("takeover_status"),
                    "flow_passed": trial.get("flow_passed"),
                    "cloud_error_types": trial.get("cloud_error_types", []),
                    "directory": directory,
                    "budget_total": budget.get("total"),
                    "budget_used": budget.get("used"),
                    "cloud_calls": recovery.get("cloud_calls", len(requests)),
                    "cloud_calls_after_handoff": recovery.get("cloud_calls_after_handoff", 0),
                    "verification_rounds": recovery.get("verification_rounds", 0),
                    "termination_reason": recovery.get("termination_reason"),
                    "error_class": recovery.get("error_class"),
                    "planner_decision": plans[-1].get("decision") if plans else None,
                    "fallback": bool(recovery.get("fallback", {}).get("reason")),
                })
        (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        # Keep a live batch report so an interrupted or stopped run still has
        # the same aggregate metrics as a normally completed run.
        _write_report(output / "report.md", summary)
        if retention == "compact":
            _compact(case)
        elif retention == "summary":
            if _compact_succeeded(case):
                _summary_only(case)
            else:
                print(f"Task evidence retained after unsuccessful environment compaction: {case}", flush=True)
                # Continuing would reuse a device whose journal still points
                # at this task's full evidence and would turn one
                # infrastructure error into repeated restore failures.
                break
        if completed_returncode != 0 and args.on_task_error == "stop":
            break
    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    _write_report(output / "report.md", summary)
    print(f"Results: {output}")
    return 0 if summary["trials"] and all(row.get("status") == "collected" for row in summary["trials"]) else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2)
