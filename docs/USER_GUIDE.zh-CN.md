# HandoffHarness 使用指南

[English](USER_GUIDE.md) | **简体中文** · [返回首页](../README.zh-CN.md)

> **文档预览**：本分支仅包含文档，指南描述待发布的源码版本；当前代码见 [main](https://github.com/Rich-King395/HandoffRecovery/tree/main)。

## 1. 准备环境

1. 克隆 [HandoffBench](https://github.com/Rich-King395/ResumeBench/tree/docs-preview)，完成其安装与环境配置。
2. 在自己的模拟器上跑通一个任务的两种基线模式。
3. 在同一个 Python 环境中安装 HandoffHarness。

建议将两个 checkout 放在同级目录：

```text
workspace/
├── ResumeBench/        # HandoffBench，含独立 .env 和 .venv
└── HandoffRecovery/    # 当前项目
```

在 `HandoffRecovery/` 中运行：

```bash
source ../ResumeBench/.venv/bin/activate
python -m pip install -e '.[cloud-agents]'
python -m pip check
cp -n .env.example .env
```

其他目录布局请使用自己的虚拟环境，并通过绝对路径 `--bench-root` 指定 Benchmark。以下命令在 HandoffRecovery 根目录运行；模型权重、App 和模拟器基线需另外准备。

## 2. 配置 Planner 与 GUI Agent

### Planner：HandoffRecovery/.env

在本机文件中编辑以下字段，将占位符替换为实际配置：

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

Planner 端点必须是**完整请求地址**，服务需要时应包含 `/chat/completions`，脚本不会自动补后缀。此处选择 Qwen；模板也提供 OpenAI、Claude、DeepSeek 的兼容服务配置块。所选端点／模型须支持图片和相应响应格式；仅在服务要求时将 `json_schema` 改为 `none`。

### GUI Agent：HandoffBench/.env

在 Benchmark checkout 中配置所选模型，与基线实验使用同一份模型配置。以 PhoneBuddy 为例：

```dotenv
PHONEBUDDY_BASE_URL=http://127.0.0.1:8001/v1
PHONEBUDDY_MODEL=PhoneBuddy-4B
PHONEBUDDY_API_KEY=EMPTY
```

单独启动模型服务，并按 Benchmark 的模型指南准备 PhoneBuddy 固定版本提示词。服务若启用认证，将 `EMPTY` 替换为实际 key。其他可选 GUI Agent 名称为 `gui_owl`、`holo`、`ui_voyager`、`qwen`、`deepseek`、`openai`、`claude`、`qwen35`、`lfm25_vl`。

示例显式使用 `handoffbench_provider_validate_configuration`，GUI 模型／端点配置由 HandoffBench 校验。切换 GUI 模型时编辑该配置；此模式下不要依赖 `--local-model`、`--local-base-url` 覆盖模型。已有 Shell 环境变量优先于 Benchmark 的 `.env`。

批测从 `--env-file`、smoke 从 `--credentials-file` 指定的文件读取 Planner 凭据。配置文件留在本机，命令传文件路径，不在命令行填写密钥。

## 3. 单任务恢复试验

指定已验证、空闲的实验环境；基线必须属于当前模拟器：

```bash
export BENCH_ROOT="$(cd ../ResumeBench && pwd)"
export DEVICE=emulator-5554
export ADB_PATH=/path/to/android-sdk/platform-tools/adb
export APPIUM_URL=http://127.0.0.1:4723
export BASELINE="$BENCH_ROOT/.runtime/environment-baselines/demo.json"
```

批测入口也可只运行一个任务，并从 `.env` 读取 Planner 配置：

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

`--dry-run` 解析配置与任务选择，不调用模型或设备，但仍要求必要配置有效，以及所选适配器需要的本地资源齐备。移除 `--dry-run` 后执行真实试验。

**注意不同入口的编号：** Recovery 批测 `--tasks 2001` 使用稳定任务 ID；Benchmark 批测 `--tasks 2`、smoke 的 `--instances 2` 使用公共编号，均指同一个 Clock 实例。smoke 支持 1–3 个实例，并要求显式提供 Planner 连接参数，详见 `python scripts/run_live_smoke.py --help`。

真实运行后，检查是否记录了 `VALID` 接管、Planner 决策、恢复 Agent 活动及可用的终态评分。未触发接管的任务不能用于证明恢复链路已跑通。

## 4. 批量运行与结果

全量实验复用上方命令，将 **`--tasks 2001` 替换为 `--all`**，使用已验证的 `full_suite_v6` 基线与新的结果根目录。若希望安全收集单任务错误后继续后续任务，使用 `--on-task-error continue`。正式恢复批测仅运行 `takeover`，每个有效任务选择首个注册场景，默认重复一次，共 200 次试验。

| 参数 | 用途 |
| --- | --- |
| `--tasks ID ...`／`--all` | 选择稳定任务 ID／全部有效任务 |
| `--repetitions N` | 每个任务重复次数 |
| `--planner-model MODEL` | 临时覆盖本批 Planner 模型 |
| `--max-decisions N` | 正整数共享 GUI 预算，起始配置使用 30 |
| `--retention detailed` | 保留逐任务证据，开发阶段建议使用 |
| `--retention compact` | 收集后删除体积较大的准备／恢复证据 |
| `--retention summary` | 汇总后删除逐任务目录 |

每次在 `--output-root` 下创建新目录，名称包含 Planner、GUI 模型、任务数、预算和唯一运行 ID。

| 输出 | 用途 |
| --- | --- |
| `report.md` | 进度与汇总结果 |
| `summary.json` | 逐任务结果、所选模型和实际开关 |
| `run.log` | 批次与子进程控制台输出 |
| 逐任务目录 | 保留时可查看详细证据及恢复诊断 |

原任务成功率与恢复成功率分别报告。比较前先检查有效接管数量、Planner／API 失败、准备／设备错误与评分可用性；模型主动终止本身不代表成功。

## 5. 保持实验可比较

示例显式选择以下起始配置，不改变历史 CLI 默认值：

| 设置 | 显式参数 |
| --- | --- |
| GUI 决策预算 | `--max-decisions 30` |
| 恢复历史 | `--recovery-history-mode isolated` |
| 附加约束／不确定进度 | 两者均为 `disabled` |
| 条件更新 | `--condition-updates disabled` |
| 条件状态来源 | `--condition-update-policy model_claim` |
| 终止方式 | `--completion-policy native` |
| Host 完成检查 | `--disable-completion-check` |

原始执行与恢复阶段共用这 30 次决策。用户接管操作不消耗或重置额度，Planner 调用另行记录。无恢复对照使用 HandoffBench，并固定相同的 GUI 模型、任务、接管策略与预算；显式控制权归还通知也需一致，缺少通知的历史基线不能直接作为严格的方法消融。

并行任务使用独立模拟器／AVD 数据、Appium 端口与 `systemPort`、基线和输出根目录。需要独立本地推理时使用独立模型服务端点／进程，并预留 GPU 资源。Planner 仍可能共享服务商额度。每组使用独立 tmux 会话，批次输出已自动保存到 `run.log`。

## 开发与验证

| 扩展方向 | 位置 |
| --- | --- |
| Planner | [Planner 适配器](https://github.com/Rich-King395/HandoffRecovery/tree/main/src/handoff_recovery/planner) |
| GUI Agent | [Executor 适配器与注册表](https://github.com/Rich-King395/HandoffRecovery/tree/main/src/handoff_recovery/executor) |
| 恢复方法 | [恢复代码包](https://github.com/Rich-King395/HandoffRecovery/tree/main/src/handoff_recovery) |
| 任务、App、接管与评分 | HandoffBench checkout |

自定义 Planner 工厂接收 `PlannerConfig`，Executor 工厂接收 `(bench_path, ExecutorSpec)`，分别通过 `--planner-adapter module:factory`、`--executor-adapter module:factory` 选择。优先参考已有适配器，保留 GUI 模型的原生接口。

实机／模型测试前先执行离线检查：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
  python -m unittest discover -s tests/unit -v
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src HANDOFFBENCH_ROOT="$BENCH_ROOT" \
  python -m unittest discover -s tests/integration -v
```

之后依次完成单任务实测、小批量和全量实验，记录模型配置、开关、预算、环境／源码版本及错误分类。测试输入应可分享，不依赖个人实验结果；详见[公开分发边界](../PUBLIC_RELEASE.md)。
