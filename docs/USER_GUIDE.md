# HandoffHarness user guide

**English** | [简体中文](USER_GUIDE.zh-CN.md) · [Home](../README.md)

> **Documentation preview:** this branch contains documentation only. Guides describe the forthcoming source release; current code is on [main](https://github.com/Rich-King395/HandoffRecovery/tree/main).

## 1. Prepare the environment

1. Clone [HandoffBench](https://github.com/Rich-King395/ResumeBench/tree/docs-preview) and follow its setup guide.
2. Confirm one task runs in both baseline modes on your emulator.
3. Install HandoffHarness into the same Python environment.

A convenient checkout layout is:

```text
workspace/
├── ResumeBench/        # HandoffBench checkout, with its own .env and .venv
└── HandoffRecovery/    # this checkout
```

From `HandoffRecovery/`:

```bash
source ../ResumeBench/.venv/bin/activate
python -m pip install -e '.[cloud-agents]'
python -m pip check
cp -n .env.example .env
```

For another layout, use your actual virtual environment and an absolute `--bench-root`. Commands below run from the HandoffRecovery root. Model weights, apps and emulator baselines are supplied separately.

## 2. Configure the Planner and GUI agent

### Planner: HandoffRecovery/.env

Edit these fields in your local file; replace the placeholders with your service settings:

```dotenv
HANDOFFBENCH_ROOT=../ResumeBench
RECOVERY_PLANNER_PROVIDER=qwen
RECOVERY_PLANNER_ADAPTER=openai_compatible
RECOVERY_PLANNER_QWEN_ENDPOINT=<complete-chat-completions-url>
RECOVERY_PLANNER_QWEN_API_KEY=<your-api-key>
RECOVERY_PLANNER_QWEN_MODEL=<your-planner-model>
RECOVERY_PLANNER_QWEN_DISABLE_THINKING=true
RECOVERY_PLANNER_QWEN_RESPONSE_FORMAT=json_schema
RECOVERY_LOCAL_AGENT=phonebuddy
RECOVERY_EXECUTOR_ADAPTER=phonebuddy
RECOVERY_MAX_DECISIONS=30
```

The Planner endpoint is the **complete request URL**, including `/chat/completions` where required. No suffix is added. The example selects Qwen; the template also provides OpenAI, Claude and DeepSeek configuration blocks for compatible services. Choose an endpoint/model that accepts images and the configured response format; use `none` instead of `json_schema` only when required by your endpoint.

### GUI agent: HandoffBench/.env

Configure the selected provider in the Benchmark checkout, just as for its baseline. For PhoneBuddy:

```dotenv
PHONEBUDDY_BASE_URL=http://127.0.0.1:8001/v1
PHONEBUDDY_MODEL=PhoneBuddy-4B
PHONEBUDDY_API_KEY=EMPTY
```

Start the model service separately and prepare PhoneBuddy's pinned prompt using its Benchmark setup guide. Replace `EMPTY` if your local endpoint requires authentication. Other supported GUI agent names are `gui_owl`, `holo`, `ui_voyager`, `qwen`, `deepseek`, `openai`, `claude`, `qwen35` and `lfm25_vl`.

The examples explicitly select `handoffbench_provider_validate_configuration`: GUI model/endpoint settings are validated by HandoffBench. Edit that configuration to change the GUI model; do not rely on `--local-model` or `--local-base-url` to override it in this mode. Existing shell variables take precedence over the Benchmark `.env`.

Planner credentials come from the file selected with `--env-file` in batch runs or `--credentials-file` in smoke runs. Keep these files local and pass paths instead of putting keys in shell commands.

## 3. Run one recovery trial

Set paths to your verified, idle environment. The baseline must belong to the selected emulator:

```bash
export BENCH_ROOT="$(cd ../ResumeBench && pwd)"
export DEVICE=emulator-5554
export ADB_PATH=/path/to/android-sdk/platform-tools/adb
export APPIUM_URL=http://127.0.0.1:4723
export BASELINE="$BENCH_ROOT/.runtime/environment-baselines/demo.json"
```

The batch entry point can run a single task and read the Planner configuration from `.env`:

```bash
python scripts/run_recovery_batch.py \
  --env-file .env --bench-root "$BENCH_ROOT" --tasks 2001 \
  --local-agent phonebuddy --executor-adapter phonebuddy \
  --configuration-source handoffbench_provider_validate_configuration \
  --device "$DEVICE" --adb "$ADB_PATH" --appium-url "$APPIUM_URL" \
  --baseline "$BASELINE" --max-decisions 30 \
  --recovery-history-mode isolated \
  --recovery-constraints disabled --recovery-uncertain disabled \
  --condition-updates disabled --condition-update-policy model_claim \
  --completion-policy native --disable-completion-check \
  --on-task-error stop --retention detailed \
  --output-root results/first-recovery --dry-run
```

`--dry-run` resolves configuration and task selection without calling a model or device. It still requires valid configuration fields and any selected adapter's local assets. Remove `--dry-run` to run the real trial.

**Numbering differs by entry point:** recovery batch `--tasks 2001` uses the stable task ID; Benchmark batch `--tasks 2` and smoke `--instances 2` use the public number for the same Clock instance. Smoke accepts 1–3 instances and takes explicit Planner connection arguments; see `python scripts/run_live_smoke.py --help`.

Check that a real run records a `VALID` takeover, a Planner decision, recovery agent activity and an available final evaluation. A task that never triggers takeover does not verify the recovery path.

## 4. Scale up and read results

For the full suite, reuse the command above with **`--all` instead of `--tasks 2001`**, a verified `full_suite_v6` baseline and a new output root. Use `--on-task-error continue` if you want safely collected task errors to allow later tasks to run. Formal recovery batches run `takeover` only, one first-registered scenario per active task, with one repetition by default: 200 trials.

| Option | Purpose |
| --- | --- |
| `--tasks ID ...` / `--all` | Selected stable task IDs / all active tasks |
| `--repetitions N` | Repetitions per task |
| `--planner-model MODEL` | Override the batch Planner model |
| `--max-decisions N` | Positive shared GUI budget; use 30 for the starting profile |
| `--retention detailed` | Keep per-task evidence; recommended while developing |
| `--retention compact` | Remove bulky preparation/restore evidence after collection |
| `--retention summary` | Remove per-task directories after aggregation |

A new run directory is created beneath `--output-root`, named with Planner, GUI model, task count, budget and a unique run ID.

| Output | Use |
| --- | --- |
| `report.md` | Progress and aggregate results |
| `summary.json` | Trial outcomes, selected models and effective switches |
| `run.log` | Batch and child-process console output |
| Per-task directories | Detailed evidence and recovery diagnostics, when retained |

Report original task success and recovery success separately. Inspect valid takeover counts, Planner/API failures, preparation/device errors and scoring availability before comparing rates. Model termination is not itself proof of success.

## 5. Keep comparisons reproducible

The examples explicitly select this starting profile; they do not change historical CLI defaults:

| Setting | Explicit value |
| --- | --- |
| GUI decision budget | `--max-decisions 30` |
| Recovery history | `--recovery-history-mode isolated` |
| Extra constraints / uncertain progress | Both `disabled` |
| Condition updates | `--condition-updates disabled` |
| Condition authority | `--condition-update-policy model_claim` |
| Termination | `--completion-policy native` |
| Host completion check | `--disable-completion-check` |

The 30 decisions are shared across the original and recovery stages. User takeover actions do not consume or reset them; Planner calls are recorded separately. For a no-recovery comparison, run HandoffBench with the same GUI model, tasks, takeover policy and budget. Explicit return-notification settings must also match; historical baselines without that notification are not strict ablations of the recovery method.

Parallel jobs need separate emulator/AVD data, Appium server and `systemPort`, baseline and output root. Use separate local model endpoints/processes for independent inference; reserve GPU capacity first. Planner endpoints may share provider quotas. Run each job in its own tmux session; batch output is already saved to `run.log`.

## Development and validation

| Extend | Location |
| --- | --- |
| Planner | [Planner adapters](https://github.com/Rich-King395/HandoffRecovery/tree/main/src/handoff_recovery/planner) |
| GUI agent | [Executor adapters and registry](https://github.com/Rich-King395/HandoffRecovery/tree/main/src/handoff_recovery/executor) |
| Recovery method | [Recovery package](https://github.com/Rich-King395/HandoffRecovery/tree/main/src/handoff_recovery) |
| Tasks, apps, takeover and scoring | The HandoffBench checkout |

A custom Planner factory receives `PlannerConfig`; a custom Executor factory receives `(bench_path, ExecutorSpec)`. Select a custom factory with `--planner-adapter module:factory` or `--executor-adapter module:factory`. Start with an existing adapter and retain the GUI model's native interface.

Run offline checks before device/model tests:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python -m unittest discover -s tests/unit -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src HANDOFFBENCH_ROOT="$BENCH_ROOT" \
  python -m unittest discover -s tests/integration -v
```

Then validate one real task, a small batch and the full suite in that order. Record model settings, switch values, budget, environment/source versions and error categories. Keep test inputs shareable and independent of personal results; see [public distribution boundary](../PUBLIC_RELEASE.md).
