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


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from handoff_recovery.core.types import MAX_CONFIGURED_TOTAL_DECISIONS

MAX_CONFIGURED_DECISIONS = MAX_CONFIGURED_TOTAL_DECISIONS


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
    parser.add_argument("--retention", choices=("detailed", "compact"))
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


def _write_report(path: Path, summary: dict) -> None:
    trials = summary["trials"]
    valid = [row for row in trials if row.get("takeover_status") == "VALID"]
    successes = sum(row.get("recovery_success") is True for row in valid)
    task_successes = sum(row.get("task_success") is True for row in valid)
    lines = [
        "# HandoffRecovery takeover batch",
        "",
        f"Planner: `{summary['planner']['provider']}` / `{summary['planner']['model']}` "
        f"(adapter `{summary['planner'].get('adapter', 'unknown')}`)",
        f"Local agent: `{summary['executor']['agent']}` / `{summary['executor']['model']}` "
        f"(adapter `{summary['executor'].get('adapter', 'unknown')}`)",
        f"Completion check: `{summary['executor'].get('completion_check_enabled', False)}`",
        f"Decision budget: `{summary.get('max_decisions', 'unknown')}`",
        f"Selected tasks: {summary['selected_task_count']}; repetitions: {summary['repetitions']}",
        "",
        "| Metric | Result |",
        "|---|---:|",
        f"| Collected trials | {len(trials)} |",
        f"| Valid takeovers | {len(valid)} |",
        f"| Task success | {task_successes}/{len(valid) if valid else 0} |",
        f"| Recovery success | {successes}/{len(valid) if valid else 0} |",
        "",
        "Detailed evidence is retained per task while `detailed` retention is enabled.",
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
            "DASHSCOPE_API_KEY" if agent_name == "qwen" else local_prefix + "API_KEY",
        ).strip(),
        "prompt_path": _path(values[local_prefix + "PROMPT_PATH"]) if values.get(local_prefix + "PROMPT_PATH") else "",
    }
    local["timeout_seconds"] = args.local_timeout_seconds or float(values.get(local_prefix + "TIMEOUT_SECONDS", "120"))
    local["total_timeout_seconds"] = args.local_total_timeout_seconds or float(values.get(local_prefix + "TOTAL_TIMEOUT_SECONDS", "120"))
    local["proxy_mode"] = args.local_proxy_mode or values.get(local_prefix + "PROXY_MODE", "environment")
    local["max_output_tokens"] = args.local_max_output_tokens or int(values.get(local_prefix + "MAX_OUTPUT_TOKENS", "2048"))
    if args.local_base_url:
        local["base_url"] = args.local_base_url
    if (agent_name != "qwen" and not local["base_url"]) or not local["model"]:
        raise ValueError("Local agent configuration is incomplete for " + agent_name)
    executor_adapter = (args.executor_adapter
                        or values.get("RECOVERY_EXECUTOR_ADAPTER", agent_name).strip()
                        or agent_name)
    max_decisions = args.max_decisions or int(values.get("RECOVERY_MAX_DECISIONS", "20"))
    if not 1 <= max_decisions <= MAX_CONFIGURED_DECISIONS:
        raise ValueError(
            f"RECOVERY_MAX_DECISIONS must be an integer from 1 to {MAX_CONFIGURED_DECISIONS}"
        )
    selected = _select_tasks(args, bench)
    retention = args.retention or values.get("RECOVERY_RESULT_RETENTION", "detailed")
    if retention not in {"detailed", "compact"}:
        raise ValueError("RECOVERY_RESULT_RETENTION must be detailed or compact")
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid.uuid4().hex[:8]
    output = (args.output_root /
              f"{_safe(planner['model'])}_{_safe(local['model'])}_"
              f"{len(selected) * args.repetitions}_{run_id}").resolve()
    output.mkdir(parents=True, exist_ok=False)
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
        if local["prompt_path"]:
            command.extend(("--local-prompt-path", local["prompt_path"]))
        print(f"[{index}/{len(plan)}] task {task_id} (public instance {public})", flush=True)
        completed = subprocess.run(command, cwd=ROOT, check=False)
        child_summary_path = case / "summary.json"
        if child_summary_path.exists():
            child = json.loads(child_summary_path.read_text())
            for trial in child.get("trials", []):
                result = trial.get("benchmark_result", {})
                summary["trials"].append({
                    "task_id": task_id, "instance_id": public,
                    "status": trial.get("status"), "task_success": result.get("task_success"),
                    "recovery_success": result.get("recovery_success"),
                    "takeover_status": result.get("takeover_status"),
                    "flow_passed": trial.get("flow_passed"),
                    "cloud_error_types": trial.get("cloud_error_types", []),
                    "directory": trial.get("directory"),
                })
        (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
        if completed.returncode != 0 and args.on_task_error == "stop":
            break
        if retention == "compact":
            _compact(case)
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
