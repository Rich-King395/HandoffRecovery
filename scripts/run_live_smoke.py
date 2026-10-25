"""Small real-model integration runs through HandoffBench's public runner.

This is an engineering smoke entry, not a full-batch evaluation command.
Only the host reads task/scenario/check configurations; the recovery agent gets
public runner messages. Credentials are read from one explicitly chosen file.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
from urllib.request import ProxyHandler, build_opener
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from handoff_recovery.core.types import DEFAULT_TOTAL_DECISIONS
from handoff_recovery.core.protocols import GUI_AGENT_ERROR_CATEGORIES, error_metadata
from handoff_recovery.core.coordinator import LifecycleError
from handoff_recovery.executor.condition_updates import experiment_metadata


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def dotenv_values(path):
    """Read the small KEY=VALUE credential files used by the smoke entry.

    Prefer python-dotenv when available, but keep this integration entry usable
    from the standard Python environment used by the formal batch wrapper.
    """
    try:
        from dotenv import dotenv_values as load_values
    except ImportError:
        values = {}
        for raw in Path(path).read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key.strip()] = value
        return values
    return load_values(path, interpolate=False)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bench-root", required=True, type=Path)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--device", required=True)
    parser.add_argument("--adb", required=True)
    parser.add_argument("--appium-url", required=True)
    parser.add_argument("--instances", type=int, nargs="+", default=[13, 14])
    parser.add_argument("--mode", choices=["takeover", "no_takeover"], default="takeover")
    parser.add_argument("--scenario-id", help="Explicit takeover scenario ID; must belong to each selected task")
    parser.add_argument("--credentials-file", type=Path)
    parser.add_argument("--planner-key-var", default="DASHSCOPE_API_KEY")
    parser.add_argument("--planner-endpoint", help="Exact full Chat Completions URL (takeover only)")
    parser.add_argument("--planner-model")
    parser.add_argument("--planner-disable-thinking", action="store_true")
    parser.add_argument(
        "--compact-environment-result",
        action="store_true",
        help="Compact the sealed environment result into the host journal before the task directory is removed",
    )
    parser.add_argument("--planner-adapter", default="openai_compatible",
                        help="Planner registry name or module:factory path")
    parser.add_argument("--planner-response-format", choices=("json_schema", "none"),
                        default="json_schema")
    parser.add_argument("--planner-debug-response-dir", type=Path,
                        help="Opt-in local directory for raw Planner response diagnostics")
    parser.add_argument("--planner-direct", action="store_true", help="Disable environment proxy for planner only")
    parser.add_argument("--max-decisions", type=int, default=DEFAULT_TOTAL_DECISIONS,
                        help=f"Shared local GUI decision budget for this run (positive integer, default {DEFAULT_TOTAL_DECISIONS})")
    parser.add_argument("--local-agent", choices=["gui_owl", "phonebuddy", "qwen", "deepseek", "openai", "claude", "ui_voyager", "holo", "ui_venus", "openmobile", "mai_gym", "qwen35", "lfm25_vl"], default="gui_owl")
    parser.add_argument("--executor-adapter",
                        help="GUI Agent registry name or module:factory path; defaults to --local-agent")
    parser.add_argument("--local-base-url", default="")
    parser.add_argument("--local-model", required=True)
    parser.add_argument("--local-key-var")
    parser.add_argument("--local-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--local-total-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--local-proxy-mode", default="environment")
    parser.add_argument("--local-max-output-tokens", type=int, default=4096)
    parser.add_argument("--recovery-history-mode", choices=("isolated", "original_snapshot"),
                        default="isolated",
                        help="Native history at recovery entry; default isolates recovery from pre-takeover history")
    parser.add_argument("--configuration-source", default="explicit_executor_spec",
                        choices=("explicit_executor_spec", "handoffbench_provider_validate_configuration"))
    parser.add_argument("--local-prompt-path", type=Path,
                        help="PhoneBuddy pinned prompt path (optional)")
    parser.add_argument("--disable-completion-check", action="store_true",
                        help="A/B control: omit and do not enforce recovery completion_check")
    parser.add_argument("--enable-completion-check", action="store_true",
                        help="Enable recovery completion_check (default: disabled)")
    parser.add_argument("--recovery-constraints", choices=("enabled", "disabled"),
                        default="disabled",
                        help="Whether Planner constraints are rendered in the GUI Agent recovery prompt (default: disabled)")
    parser.add_argument("--recovery-uncertain", choices=("enabled", "disabled"),
                        default="disabled",
                        help="Whether Planner uncertain progress is rendered in the GUI Agent recovery prompt (default: disabled)")
    parser.add_argument("--condition-updates", choices=("enabled", "disabled"),
                        help="Independent condition-claim switch; omitted preserves existing adapter behavior")
    parser.add_argument("--completion-policy", choices=("conditions", "native"), default="conditions",
                        help="Recovery termination: existing condition gate or native model completion")
    parser.add_argument("--condition-update-policy", choices=("host", "model_claim"),
                        default="model_claim",
                        help="Who marks model-claimed recovery conditions satisfied; default model_claim")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.local_agent == "mai_gym":
        supplied = {arg.split("=", 1)[0] for arg in (sys.argv[1:] if argv is None else argv)}
        if (args.configuration_source == "explicit_executor_spec"
                and "--local-max-output-tokens" not in supplied):
            parser.error("MAIGym requires an explicit --local-max-output-tokens")
        if "--local-timeout-seconds" not in supplied:
            args.local_timeout_seconds = 600.0
        if "--local-total-timeout-seconds" not in supplied:
            args.local_total_timeout_seconds = 600.0
    if args.local_agent == "openmobile":
        supplied = {arg.split("=", 1)[0] for arg in (sys.argv[1:] if argv is None else argv)}
        if (args.configuration_source == "explicit_executor_spec"
                and "--local-max-output-tokens" not in supplied):
            parser.error("OpenMobile requires an explicit --local-max-output-tokens")
        if "--local-timeout-seconds" not in supplied:
            args.local_timeout_seconds = 300.0
        if "--local-total-timeout-seconds" not in supplied:
            args.local_total_timeout_seconds = 300.0
    if args.local_agent == "ui_venus" and not any(
            arg.split("=", 1)[0] == "--local-max-output-tokens"
            for arg in (sys.argv[1:] if argv is None else argv)):
        args.local_max_output_tokens = 16384
    if args.local_agent in {"ui_venus", "openmobile", "mai_gym"} and (args.condition_updates != "disabled"
            or args.completion_policy != "native" or args.enable_completion_check):
        parser.error(("UI-Venus" if args.local_agent == "ui_venus" else "MAIGym" if args.local_agent == "mai_gym" else "OpenMobile")
                     + " requires --condition-updates disabled --completion-policy native without completion_check")
    if args.completion_policy == "native" and args.enable_completion_check:
        parser.error("--completion-policy native cannot enable completion_check")
    if (args.condition_updates == "disabled" and args.completion_policy == "conditions"
            and args.condition_update_policy == "model_claim" and not args.enable_completion_check):
        parser.error("Disabled condition updates need --completion-policy native, host claims, or completion_check")
    if args.disable_completion_check and args.enable_completion_check:
        parser.error("--disable-completion-check and --enable-completion-check are mutually exclusive")
    if not 1 <= len(args.instances) <= 3 or len(set(args.instances)) != len(args.instances):
        parser.error("Choose one to three distinct public instances for this smoke entry")
    if args.mode == "takeover" and not all((args.credentials_file, args.planner_endpoint, args.planner_model)):
        parser.error("takeover requires --credentials-file, --planner-endpoint and --planner-model")
    if args.local_agent != "qwen" and not args.local_base_url:
        parser.error("--local-base-url is required for this local agent")
    if args.max_decisions < 1:
        parser.error("--max-decisions must be a positive integer")
    if args.local_timeout_seconds <= 0 or args.local_total_timeout_seconds <= 0:
        parser.error("local timeouts must be positive")
    if args.local_max_output_tokens < 1:
        parser.error("--local-max-output-tokens must be positive")
    if args.scenario_id and args.mode != "takeover":
        parser.error("--scenario-id requires takeover mode")
    return args


class LoggedPlanner:
    def __init__(self, inner):
        self.inner = inner
        self.attempts = 0
        self.errors = []

    def plan(self, request):
        self.attempts += 1
        print(f"  cloud {self.attempts}: requesting state analysis", flush=True)
        try:
            reply = self.inner.plan(request)
        except Exception as exc:
            # These clients sanitize errors; retain the type in smoke summaries.
            status = getattr(exc, "http_status", None)
            metadata = error_metadata(exc, default_category="planner_error", source="planner")
            error = {"type": metadata["error_type"], "category": metadata["error_category"],
                     "error_class": metadata["error_class"]}
            if type(status) is int:
                error["http_status"] = status
            self.errors.append(error)
            print(f"  cloud {self.attempts}: {error}", flush=True)
            raise
        print(f"  cloud {self.attempts}: {reply.plan.decision}", flush=True)
        return reply


class DisabledPlanner:
    """A normal-mode control must never call any cloud client."""
    descriptor = {"provider": "disabled_no_takeover"}

    def plan(self, request):
        raise RuntimeError("Cloud calls are disabled for no_takeover smoke runs")


def _descriptor(adapter, fallback):
    value = getattr(adapter, "descriptor", None)
    return value if isinstance(value, dict) else {"adapter": fallback}


def assert_appium_idle(url):
    # Device lock also protects the baseline and lifecycle subprocesses.
    opener = build_opener(ProxyHandler({}))
    with opener.open(url.rstrip("/") + "/appium/sessions", timeout=10) as response:
        sessions = json.load(response)["value"]
    if not isinstance(sessions, list) or sessions:
        raise RuntimeError("Selected Appium has an active or unknown session; stop before restoration")


def main(argv=None):
    args = parse_args(argv)
    bench = args.bench_root.resolve()
    from handoff_recovery.integrations.handoffbench.loader import (
        load_gui_owl_bindings, load_phonebuddy_bindings,
    )
    if args.local_agent == "phonebuddy":
        load_phonebuddy_bindings(bench)
    elif args.local_agent == "gui_owl":
        load_gui_owl_bindings(bench)
    elif args.local_agent == "ui_voyager":
        from handoff_recovery.integrations.handoffbench.loader import load_ui_voyager_bindings
        load_ui_voyager_bindings(bench)
    elif args.local_agent == "mai_gym":
        from handoff_recovery.integrations.handoffbench.loader import load_mai_gym_bindings
        load_mai_gym_bindings(bench)
    elif args.local_agent == "openmobile":
        from handoff_recovery.integrations.handoffbench.loader import load_openmobile_bindings
        load_openmobile_bindings(bench)
    elif args.local_agent == "ui_venus":
        from handoff_recovery.integrations.handoffbench.loader import load_ui_venus_bindings
        load_ui_venus_bindings(bench)
    elif args.local_agent == "holo":
        from handoff_recovery.integrations.handoffbench.loader import load_holo_bindings
        load_holo_bindings(bench)
    else:
        from handoff_recovery.integrations.handoffbench.loader import load_cloud_bindings
        load_cloud_bindings(bench, args.local_agent)
    sys.path.insert(0, str(bench))
    from handoff_recovery.planner import PlannerConfig, create_planner
    from handoff_recovery.executor import ExecutorSpec, create_executor
    from handoff_recovery.integrations.handoffbench.agent import HandoffRecoveryAgent
    from resumebench.core.config import load_suite
    from resumebench.core.numbering import resolve_task_id, ensure_active_task_ids
    from resumebench.core.runner import run_task
    from resumebench.batch.planning import adapter
    from resumebench.batch.calendar_date_binding import (
        load_prepared_date, requires_prepared_date,
    )
    from resumebench.environments.baseline import load_baseline
    from resumebench.environments.snapshot import SnapshotBackend
    from resumebench.environments.manager import EnvironmentManager
    from resumebench.devices.appium import AppiumDevice
    from scripts.run_resumebench_batch import device_lock, execute_stage, collect
    import yaml

    # No global environment mutation, no copying credentials to configs/results.
    values = dotenv_values(args.credentials_file) if args.credentials_file else {}
    cloud_key = values.get(args.planner_key_var) or ""
    local_key_var = args.local_key_var or {
        "phonebuddy": "PHONEBUDDY_API_KEY", "gui_owl": "GUI_OWL_API_KEY",
        "qwen": "DASHSCOPE_API_KEY", "deepseek": "DEEPSEEK_API_KEY",
        "openai": "OPENAI_API_KEY", "claude": "CLAUDE_API_KEY",
        "ui_voyager": "UI_VOYAGER_API_KEY", "holo": "HOLO_API_KEY",
        "ui_venus": "UI_VENUS_API_KEY", "openmobile": "OPENMOBILE_API_KEY", "mai_gym": "MAI_GYM_API_KEY",
        "qwen35": "QWEN35_API_KEY", "lfm25_vl": "LFM25_VL_API_KEY",
    }[args.local_agent]
    local_key = values.get(local_key_var) or "EMPTY"
    cloud_config = (PlannerConfig(endpoint=args.planner_endpoint, model=args.planner_model,
        api_key=cloud_key, enable_thinking=False if args.planner_disable_thinking else None,
        response_format_mode=args.planner_response_format,
        debug_response_dir=str(args.planner_debug_response_dir.resolve()) if args.planner_debug_response_dir else None,
        use_environment_proxy=not args.planner_direct) if args.mode == "takeover" else None)
    local_config = ExecutorSpec(
        agent=args.local_agent, base_url=args.local_base_url, model=args.local_model,
        api_key=local_key, prompt_path=(args.local_prompt_path.resolve()
                                        if args.local_prompt_path else None),
        timeout_seconds=args.local_timeout_seconds,
        total_timeout_seconds=args.local_total_timeout_seconds,
        proxy_mode=args.local_proxy_mode,
        max_tokens=args.local_max_output_tokens,
        configuration_source=args.configuration_source,
        recovery_history_mode=args.recovery_history_mode,
        include_recovery_constraints=(args.recovery_constraints == "enabled"),
        include_recovery_uncertain=(args.recovery_uncertain == "enabled"),
                    enable_completion_check=bool(args.enable_completion_check))
    if (args.configuration_source == "handoffbench_provider_validate_configuration"
            and args.local_agent in {"gui_owl", "phonebuddy", "ui_voyager", "holo", "ui_venus", "openmobile", "mai_gym"}):
        from dataclasses import replace
        from handoff_recovery.executor.registry import load_local_configuration
        selected = load_local_configuration(bench, args.local_agent)
        local_config = replace(local_config, api_key=selected.api_key, model=selected.model,
            base_url=selected.base_url, timeout_seconds=selected.timeout_seconds,
            max_tokens=selected.max_tokens,
            prompt_path=getattr(selected, "prompt_path", None))
    elif args.local_agent in {"qwen35", "lfm25_vl"}:
        from dataclasses import replace
        from handoff_recovery.integrations.handoffbench.loader import load_cloud_bindings
        selected = load_cloud_bindings(bench, args.local_agent).validate_configuration()
        local_config = replace(local_config, api_key=selected.api_key, model=selected.model,
            base_url=selected.base_url, timeout_seconds=selected.timeout_seconds,
            total_timeout_seconds=selected.total_timeout_seconds, proxy_mode=selected.proxy_mode,
            max_tokens=selected.max_output_tokens,
            configuration_source="handoffbench_provider_validate_configuration")
    del values, cloud_key, local_key
    def make_planner():
        return (create_planner(args.planner_adapter, cloud_config)
                if cloud_config is not None else DisabledPlanner())
    executor_adapter = args.executor_adapter or args.local_agent
    tasks_path = bench / "resumebench/configs/tasks.json"
    recipes = yaml.safe_load((bench / "resumebench/configs/recipes.yaml").read_text())["recipes"]
    contexts = []
    for number in args.instances:
        task_id = resolve_task_id(number, "public")
        ensure_active_task_ids([task_id])
        recipe = next(row for row in recipes if row["task_id"] == task_id)
        scenario_id = args.scenario_id if args.mode == "takeover" else None
        if scenario_id is not None:
            scenario_ids = {row["id"] for row in recipe["scenarios"]}
            if scenario_id not in scenario_ids:
                raise ValueError(f"Scenario {scenario_id!r} is not registered for task {task_id}")
        elif args.mode == "takeover":
            scenario_id = recipe["scenarios"][0]["id"]
        suite = load_suite(tasks_path, mode=args.mode, scenario_id=scenario_id)
        task = next(row for row in suite["tasks"] if row["id"] == task_id)
        if recipe["adapter"] not in {"legacy_v1", "apps_v1", "online_v1"}:
            raise ValueError("Smoke entry currently supports legacy_v1/apps_v1/online_v1 preparation only")
        calendar_binding = requires_prepared_date(task, suite["checks"])
        contexts.append((number, task, recipe, suite, calendar_binding))
    manifest = load_baseline(args.baseline, device=args.device,
                             task_ids=[task["id"] for _, task, _, _, _ in contexts])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid.uuid4().hex[:8]
    output = (args.output or ROOT / "results" / ("live_smoke_" + stamp)).resolve()
    output.mkdir(parents=True, exist_ok=False)
    source_paths = [*sorted((ROOT / "src").rglob("*.py")),
                    ROOT / "src/handoff_recovery/planner/system_prompt.txt", Path(__file__),
                    bench / "resumebench/core/agent_protocol.py", bench / "resumebench/core/runner.py",
                    bench / ("resumebench/agents/phonebuddy.py" if args.local_agent == "phonebuddy"
                             else ("resumebench/agents/gui_owl.py" if args.local_agent == "gui_owl"
                                   else "resumebench/agents/" + args.local_agent + ".py")),
                    bench / ("resumebench/models/api_method_phonebuddy.py" if args.local_agent == "phonebuddy"
                             else ("resumebench/models/api_method_gui_owl.py" if args.local_agent == "gui_owl"
                                   else "resumebench/models/api_method_" + args.local_agent + ".py")),
                    bench / "resumebench/batch/calendar_date_binding.py",
                    bench / "resumebench/batch/online.py",
                    *sorted((bench / "resumebench/configs").glob("*.*"))]
    if args.local_agent not in {"phonebuddy", "gui_owl"}:
        source_paths.extend((bench / "resumebench/agents/cloud.py",
                             bench / "resumebench/models/cloud_common.py",
                             bench / "resumebench/models/gui_policy.py"))
        if args.local_agent == "qwen":
            source_paths.extend((bench / "resumebench/models/qwen_model.py",
                                 bench / "resumebench/models/qwen_transport.py"))
        if args.local_agent in {"qwen35", "lfm25_vl"}:
            source_paths.extend((bench / "resumebench/agents/local_vlm.py",
                                 bench / "resumebench/models/local_vlm.py"))
    if args.local_agent == "mai_gym":
        source_paths.extend(bench / "resumebench/models/prompts" / name for name in (
            "mai_gym_mobile_use_windows_wise_system.j2", "mai_gym_source.json"))
    if args.local_agent == "openmobile":
        source_paths.extend(bench / "resumebench/models/prompts" / name for name in (
            "openmobile_2_system.txt", "openmobile_2_source.json"))
    if args.local_agent == "ui_venus":
        source_paths.extend(bench / "resumebench/models/prompts" / name for name in (
            "ui_venus_2_system.txt", "ui_venus_2_source.json"))
    sources = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in source_paths if path.is_file()}
    summary = {"scope": "real_models_public_runner_functional_smoke_not_full_evaluation",
        "started_at": datetime.now(timezone.utc).isoformat(), "device": args.device,
        "appium_url": args.appium_url, "baseline_id": manifest["baseline_id"],
        "mode": args.mode, "planner": _descriptor(make_planner(), args.planner_adapter),
        "executor": {"agent": args.local_agent, "model": local_config.model,
                      "adapter": executor_adapter,
                      "base_url": local_config.base_url,
                      "configuration_source": local_config.configuration_source,
                      **({"timeout_seconds": local_config.timeout_seconds,
                          "max_output_tokens": local_config.max_tokens}
                         if args.local_agent in {"gui_owl", "phonebuddy", "ui_voyager", "holo", "ui_venus", "openmobile", "mai_gym", "qwen35", "lfm25_vl"} else {}),
                      "recovery_history_mode": args.recovery_history_mode,
                      "recovery_constraints": args.recovery_constraints,
                      "recovery_uncertain": args.recovery_uncertain,
                      "condition_update_policy": args.condition_update_policy,
                      **experiment_metadata(args.condition_updates, args.local_agent, args.completion_policy),
                      "completion_policy": args.completion_policy,
                      "completion_check_enabled": bool(args.enable_completion_check)},
        "max_decisions": args.max_decisions, "sources": sources, "trials": []}
    write(output / "summary.json", summary)
    print(f"Results: {output}", flush=True)
    with device_lock(args.device) as lock_fd:
        assert_appium_idle(args.appium_url)
        backend = SnapshotBackend(manifest, adb_path=args.adb, appium_url=args.appium_url, lock_fd=lock_fd)
        manager = EnvironmentManager(manifest, backend=backend)
        for number, task, recipe, suite, calendar_binding in contexts:
            case = output / f"instance_{number}_{args.mode}"
            case.mkdir()
            item = {"instance_id": number, "task_id": task["id"], "status": "running", "directory": str(case)}
            summary["trials"].append(item)
            write(output / "summary.json", summary)
            started = time.monotonic()
            active = False
            device = None
            agent = None
            planner = LoggedPlanner(make_planner())
            prepared_calendar_date = None
            calendar_manifest = None
            calendar_manifest_sha256 = None
            try:
                executor = create_executor(executor_adapter, bench, local_config)
                descriptor = getattr(executor, "descriptor", None)
                if isinstance(descriptor, dict):
                    summary["executor"]["descriptor"] = descriptor
                    # Cloud adapters resolve model and endpoint from the
                    # selected HandoffBench provider configuration. Mirror
                    # that authoritative metadata in the run summary.
                    if descriptor.get("configuration_source", "").startswith("handoffbench"):
                        summary["executor"]["model"] = descriptor.get("model")
                        summary["executor"]["base_url_host"] = descriptor.get("base_url_host")
                        summary["executor"]["configuration_source"] = descriptor["configuration_source"]
                    write(output / "summary.json", summary)
                agent = HandoffRecoveryAgent(
                    bench_path=bench, planner=planner, executor=executor,
                    action_validator=getattr(executor, "validate_action", None),
                    condition_update_policy=args.condition_update_policy,
                    completion_policy=args.completion_policy,
                    condition_updates_enabled=(None if args.condition_updates is None
                                               else args.condition_updates == "enabled"))
                print(f"Instance {number}: restoring baseline", flush=True)
                backend.set_task_context(task["id"])
                restored = manager.restore(case / "restore", reason="handoff_recovery_functional_smoke",
                                           verification_level="light")
                if restored.get("passed") is not True:
                    raise RuntimeError("Environment restore did not pass")
                manager.begin_trial(f"recovery_smoke_{stamp}_{number}", directory=case)
                active = True
                module = adapter(recipe["adapter"])
                stage = module.build_stage(task["id"], "prepare", state="baseline",
                    scenario_id=suite["scenario"]["id"] if suite["scenario"] else None,
                    initialize=True, device_args={"device": args.device, "adb": args.adb,
                    "appium_url": args.appium_url, "tasks_config": tasks_path},
                    output=case / "preparation" / "results", python=sys.executable)
                print(f"Instance {number}: preparing task", flush=True)
                code = execute_stage(stage, lock_fd=lock_fd)
                path, prepared = collect(stage)
                module.verify_stage(path, prepared, code, task=task, suite=suite, state="baseline", phase="prepare")
                reader = getattr(module, "extract_preparation_receipt", module.extract_manifest)
                preparation_manifest = reader(prepared)
                if preparation_manifest is None:
                    raise RuntimeError("Preparation did not return a fixture ownership manifest")
                calendar_manifest = Path(preparation_manifest).resolve()
                manager.mark_prepared(calendar_manifest)
                if task["app"] == "com.google.android.calendar":
                    calendar_manifest_sha256 = hashlib.sha256(calendar_manifest.read_bytes()).hexdigest()
                    item["calendar_preparation"] = {
                        "manifest_sha256": calendar_manifest_sha256,
                        "date_binding": calendar_binding,
                    }
                if calendar_binding:
                    prepared_calendar_date = load_prepared_date(
                        calendar_manifest, calendar_manifest_sha256,
                        task_id=task["id"], device=args.device)
                manager.mark_running()
                device = AppiumDevice(args.device, adb_path=args.adb, appium_url=args.appium_url,
                    width=task["environment"]["width"], height=task["environment"]["height"])
                print(f"Instance {number}: running real models", flush=True)
                result = run_task(task=task, mode=args.mode, device=device, agent=agent,
                    checker=suite["checks"], run_dir=case / "benchmark", scenario=suite["scenario"],
                    flow=suite["flow"], max_decisions=args.max_decisions, preflight=True,
                    prepared_calendar_date=prepared_calendar_date,
                    metadata={"scope": summary["scope"], "agent": "handoff_recovery",
                              "planner": summary["planner"], "executor": summary["executor"]})
                item["benchmark_result"] = result
                if calendar_binding:
                    item["calendar_preparation"] = {
                        "manifest_sha256": calendar_manifest_sha256,
                        "date_binding": True,
                        "prepared_date": prepared_calendar_date,
                    }
                item["status"] = ("collected" if result["termination_reason"] in
                    {"agent_done", "agent_failed", "budget_exhausted"} else "error")
            except Exception as exc:
                category = getattr(exc, "category", None)
                if isinstance(exc, LifecycleError):
                    source = "lifecycle"
                elif isinstance(category, str) and (category in GUI_AGENT_ERROR_CATEGORIES
                                                    or category == "integration_error"):
                    # The public bridge re-raises a sanitized GUI Agent error;
                    # preserve its protocol category instead of treating it as
                    # an Appium/environment failure.
                    source = "executor"
                elif isinstance(category, str) and category in {
                        "planner_error", "plan_validation_error",
                        "planner_unavailable_or_invalid"}:
                    source = "planner"
                else:
                    source = "environment"
                metadata = error_metadata(exc, default_category=(
                    "lifecycle_error" if source == "lifecycle" else
                    "executor_contract_error" if source == "executor" else
                    "planner_error" if source == "planner" else "environment_error"),
                    source=source)
                item.update(status="error", error_type=metadata["error_type"],
                            error_category=metadata["error_category"],
                            error_class=metadata["error_class"],
                            error_message=str(exc)[:240])
                print(f"Instance {number}: {type(exc).__name__}", flush=True)
            finally:
                if device is not None:
                    try:
                        device.close()
                    except Exception as exc:
                        metadata = error_metadata(exc, default_category="environment_error",
                                                  source="environment")
                        item.update(status="error", cleanup_error_type=metadata["error_type"],
                                    cleanup_error_category=metadata["error_category"],
                                    cleanup_error_class=metadata["error_class"])
                recovery = (agent.report() if agent is not None and agent.coordinator is not None else
                            {"scope": "handoffbench_public_protocol", "phase": "not_started",
                             "task_success": None})
                write(case / "recovery.json", recovery)
                if recovery.get("error_category") and not item.get("error_category"):
                    item["error_category"] = recovery["error_category"]
                if recovery.get("error_class") and not item.get("error_class"):
                    item["error_class"] = recovery["error_class"]
                if recovery.get("error_type") and not item.get("error_type"):
                    item["error_type"] = recovery["error_type"]
                item.update(seconds=round(time.monotonic() - started, 3), cloud_request_attempts=planner.attempts,
                            cloud_errors=planner.errors,
                            cloud_error_types=[error.get("type") for error in planner.errors],
                            cloud_error_categories=[error.get("category") for error in planner.errors],
                            cloud_error_classes=[error.get("error_class") for error in planner.errors])
                result = item.get("benchmark_result", {})
                item["flow_passed"] = bool(item["status"] == "collected"
                    and result.get("evaluation_status") == "ok"
                    and recovery.get("budget", {}).get("used") == sum(result.get("counts", {}).get("decisions", {}).values())
                    and recovery.get("cloud_calls_after_handoff") == 0
                    and not planner.errors
                    and (args.mode == "no_takeover" or (result.get("takeover_status") == "VALID"
                         and planner.attempts > 0)))
                write(case / "smoke.json", item)
                if active:
                    manager.seal_result(item, evidence_paths=[case])
                    if args.compact_environment_result:
                        # The environment journal is bounded. Reuse the same
                        # compact trial projection as HandoffBench instead of
                        # writing recovery history and raw evidence metadata.
                        from resumebench.batch.retention import compact_trial
                        receipt = manager.compact_result(compact_trial(item))
                        compact_marker = case / "environment/compact-result-receipt.json"
                        compact_marker.parent.mkdir(parents=True, exist_ok=True)
                        write(compact_marker, receipt)
                write(output / "summary.json", summary)
            print(f"Instance {number}: {item['status']}", flush=True)
            if item["status"] == "error":
                break  # Infrastructure failure: do not launch another paid trial.
    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    write(output / "summary.json", summary)
    return 0 if all(row["flow_passed"] for row in summary["trials"]) else 2


if __name__ == "__main__":
    raise SystemExit(main())
