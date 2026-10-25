# HandoffHarness

**English** | [简体中文](README.zh-CN.md)

Recovery assistance for Android GUI agents when a user returns control after an intervention.

Use your own Planner and GUI agent with the tasks in [HandoffBench](https://github.com/Rich-King395/ResumeBench). The repository and Python package retain the names `HandoffRecovery` and `handoff_recovery`.

## 🧭 What can I run?

| Goal | Entry point |
| --- | --- |
| Test recovery on 1–3 instances | `scripts/run_live_smoke.py` |
| Run recovery over selected tasks or the full suite | `scripts/run_recovery_batch.py` |
| Run the no-recovery baseline in either takeover mode | [HandoffBench](https://github.com/Rich-King395/ResumeBench) |

HandoffBench currently provides **18 apps and 200 active task instances**. Use the same task set, environment and GUI decision budget for comparisons. The recommended starting profile uses native agent termination with condition updates and the completion check disabled.

New server or collaborator? Start with the [collaborator setup guide](docs/COLLABORATION.md).

## 🚀 Get started

First prepare HandoffBench, its Android environment and a working GUI agent. Then install HandoffHarness in the **same Python environment**:

```bash
git clone https://github.com/Rich-King395/HandoffRecovery.git
cd HandoffRecovery
python -m pip install -e '.[cloud-agents]'
python scripts/run_recovery_batch.py --help
```

HandoffBench supplies the Android and model-client dependencies; this package alone does not install a complete experiment environment.

Follow the [user guide](docs/USER_GUIDE.md) to configure a Planner, select your Benchmark checkout, and run a first recovery trial.

## 🤖 Choose your models

| Role | Choices |
| --- | --- |
| Planner | A configured OpenAI-compatible Chat Completions service, or a custom Planner adapter |
| Commercial GUI agent | `qwen`, `deepseek`, `openai`, `claude` |
| Specialized GUI agent | `phonebuddy`, `holo`, `gui_owl`, `ui_voyager`, `ui_venus`, `openmobile`, `mai_gym` |
| General local vision model | `qwen35`, `lfm25_vl` |

UI-Venus-2-9B (`ui_venus`), OpenMobile-2-9B (`openmobile`) and MAI-Gym-9B (`mai_gym`) are registered in both projects with their native protocols. Use native completion, disabled condition updates and disabled completion checks. OpenMobile and MAI-Gym require an explicit output token limit. Configure your own compatible model endpoint and run a single-task smoke before a full experiment. LFM2.5-VL remains experimental.

## 🛠 Develop your own method

- Add a Planner or GUI agent adapter.
- Compare recovery settings with a fixed task set, model and shared budget.
- Reuse HandoffBench tasks and independent scoring for your experiments.

See [development and validation](docs/USER_GUIDE.md#development-and-validation) for extension points and offline checks.

## Documentation

[User guide](docs/USER_GUIDE.md) · [Command index](scripts/README.md) · [Public distribution boundary](PUBLIC_RELEASE.md) · [HandoffBench](https://github.com/Rich-King395/ResumeBench)

[Paired source revision](docs/COMPATIBILITY.md).
