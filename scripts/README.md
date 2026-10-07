# 运行入口目录

## 少量真实模型联调

`run_live_smoke.py` 通过 Benchmark 的公共 `handoff_v1` 协议运行独立恢复 Agent，支持 `takeover`／`no_takeover`，每次只允许 1～3 个不同公开实例。真实服务与设备验收仍以实际运行记录为准；脚本存在不代表链路已验收。

## 正式 takeover 批量入口

批量入口支持 `--planner-model` 临时覆盖 `.env` 中的 Planner 模型，便于在不复制凭据的情况下进行模型 A/B 实验；支持 `--max-decisions` 或 `RECOVERY_MAX_DECISIONS` 配置共享 GUI 决策预算。并行运行时必须为每个进程指定独立的 `--device`、`--appium-url` 和结果根目录。

`run_recovery_batch.py` 面向 HandoffRecovery 的正式恢复实验，模式固定为 `takeover`。它只接受稳定的 HandoffBench `task_id`，或者使用 `--all` 选择全部活动任务；public instance 编号仅在内部转换并写入结果元数据。

逐项 `completion_check` 的小范围消融实验使用 `--disable-completion-check` 作为控制组开关。控制组会同时去掉 PhoneBuddy 恢复提示中的该要求，并关闭 Coordinator 的强制校验；实验组省略该开关。两组必须使用独立的本地模型服务、Android serial、Appium URL 和结果目录。Planner 云端端点可以相同，但每个批次仍应使用独立结果目录。

例如，对此前 11 个重点任务运行控制组和实验组：

```bash
# 控制组：不启用 completion_check，独立 PhoneBuddy 服务/设备/Appium
PYTHONPATH=src python3 scripts/run_recovery_batch.py \
  --env-file .env \
  --tasks 2001 2003 2012 2013 2014 2028 2089 2140 2152 2153 2183 \
  --local-base-url http://127.0.0.1:8001/v1 \
  --device emulator-5572 \
  --appium-url http://127.0.0.1:4741 \
  --baseline ../ResumeBench/HandoffBench/.runtime/environment-baselines/full-worker7-v1.json \
  --output-root results/ab_completion_control \
  --disable-completion-check \
  --planner-debug-response-dir results/ab_completion_control/planner_debug

# 实验组：启用 completion_check，独立 PhoneBuddy 服务/设备/Appium
PYTHONPATH=src python3 scripts/run_recovery_batch.py \
  --env-file .env \
  --tasks 2001 2003 2012 2013 2014 2028 2089 2140 2152 2153 2183 \
  --local-base-url http://127.0.0.1:8002/v1 \
  --device emulator-5574 \
  --appium-url http://127.0.0.1:4743 \
  --baseline ../ResumeBench/HandoffBench/.runtime/environment-baselines/full-worker8-v1.json \
  --output-root results/ab_completion_enabled \
  --planner-debug-response-dir results/ab_completion_enabled/planner_debug
```

启动批处理前，需分别确认 `8001`、`8002` 是两个独立 PhoneBuddy 服务，且 Worker7 (`emulator-5572`/`4741`) 与 Worker8 (`emulator-5574`/`4743`) 不被其它实验占用。两组应使用同一任务集合、同一 Planner 配置；设备使用各自对应的 `full-worker7-v1.json`／`full-worker8-v1.json`，不能交叉使用 baseline。

两个 PhoneBuddy 服务应分别固定到不同 GPU，并在独立终端或 tmux 会话中启动：

```bash
# GPU 0 -> 控制组服务 8001
CUDA_VISIBLE_DEVICES=0 PYTHONPATH=/home/rich/MobileAgent/ResumeBench/HandoffBench \
python3 /home/rich/MobileAgent/ResumeBench/HandoffBench/scripts/serve_phonebuddy.py \
  --model-path /home/rich/MobileAgent/local-models/phonebuddy-4b/model \
  --served-model-name PhoneBuddy-4B --port 8001

# GPU 1 -> completion_check 实验组服务 8002
CUDA_VISIBLE_DEVICES=1 PYTHONPATH=/home/rich/MobileAgent/ResumeBench/HandoffBench \
python3 /home/rich/MobileAgent/ResumeBench/HandoffBench/scripts/serve_phonebuddy.py \
  --model-path /home/rich/MobileAgent/local-models/phonebuddy-4b/model \
  --served-model-name PhoneBuddy-4B --port 8002
```

Appium 也必须各自绑定 Worker7/8：

```bash
cd /home/rich/MobileAgent/ResumeBench/HandoffBench
APPIUM_PORT=4741 APPIUM_SYSTEM_PORT=8218 APPIUM_DEVICE_UDID=emulator-5572 \
APPIUM_SESSION_DISCOVERY=1 bash scripts/start_appium.sh

APPIUM_PORT=4743 APPIUM_SYSTEM_PORT=8219 APPIUM_DEVICE_UDID=emulator-5574 \
APPIUM_SESSION_DISCOVERY=1 bash scripts/start_appium.sh
```

实验配置从 `HandoffRecovery/.env` 读取。Planner 使用 `RECOVERY_PLANNER_PROVIDER` 选择凭据和模型配置块，并使用 `RECOVERY_PLANNER_ADAPTER` 选择适配器；GUI Agent 使用 `RECOVERY_LOCAL_AGENT` 选择凭据和模型配置块，并使用 `RECOVERY_EXECUTOR_ADAPTER` 选择适配器。内置适配器包括 `phonebuddy`、`gui_owl`、`qwen`、`deepseek`、`openai`、`claude`、`ui_voyager` 和 `holo`，也可传入 `module:factory` 动态加载自定义适配器。共享云端适配器复用 HandoffBench 的视觉动作协议；`ui_voyager` 和 `holo` 保留各自原生工具协议，并复用 HandoffBench 的解析、映射和请求实现。

自定义 Planner 工厂签名为 `factory(PlannerConfig) -> PlannerProtocol`；自定义 GUI Agent 工厂签名为 `factory(bench_path, ExecutorSpec) -> ExecutorProtocol`。两者都必须实现核心协议中的 `plan`、`start_task` 和 `decide`，Coordinator 不依赖具体模型类名。

Executor 的统一恢复契约要求 `start_task` 和 `decide` 两个方法；注册表会在创建适配器时拒绝缺少方法的对象。适配器可以额外提供 `validate_action`、`descriptor` 和 `requires_completion_check`，用于动作校验、实验元数据和完成检查能力声明。

选定任务的 dry-run：

```bash
PYTHONPATH=src python3 scripts/run_recovery_batch.py \
  --tasks 2057 2093 --dry-run
```

真实运行：

```bash
PYTHONPATH=src python3 scripts/run_recovery_batch.py \
  --tasks 2057 2093
```

将 HandoffBench 的共享视觉 Agent 作为恢复执行器时，在 `.env` 中选择对应适配器：

```env
RECOVERY_LOCAL_AGENT=qwen
RECOVERY_EXECUTOR_ADAPTER=qwen
QWEN_BASE_URL=
QWEN_MODEL=qwen-vl-model
DASHSCOPE_API_KEY=...
```

`qwen` 的空 `QWEN_BASE_URL` 使用 HandoffBench 的 DashScope 原生客户端；
`deepseek`、`openai`、`claude`、`ui_voyager` 和 `holo` 分别读取对应前缀配置。所有适配器都仍需使用独立的设备、Appium、基线和结果目录，
以避免并行实验共享运行时服务。

默认 Qwen GUI Agent 凭据变量是 `DASHSCOPE_API_KEY`。如果它与 Planner
复用同一凭据，可设置 `QWEN_API_KEY_VAR=RECOVERY_PLANNER_QWEN_API_KEY`；
运行器会读取该变量名对应的值，不会把密钥复制到结果或配置元数据中。

定位 Planner 结构化输出问题时，可显式保存本地调试响应；该选项默认关闭，调试目录不写入批量汇总：

```bash
PYTHONPATH=src python3 scripts/run_recovery_batch.py \
  --tasks 2057 2093 \
  --planner-debug-response-dir /tmp/handoffrecovery-planner-debug
```

每个失败响应会保存原始计划文本及脱敏结构诊断；调试捕获不会改变 fallback 行为。

全量运行：

```bash
PYTHONPATH=src python3 scripts/run_recovery_batch.py --all
```

结果目录格式为：

```text
results/<planner_model>_<local_model>_<task_count>_<run_id>/
```

恢复方案开发阶段默认使用 `RECOVERY_RESULT_RETENTION=detailed`，每个任务保留独立诊断目录；方案稳定后可改为 `compact`，删除 preparation 和 restore 等大体积生命周期证据，只保留汇总、任务结果和恢复诊断。HandoffBench 的 preparation、Runner、takeover 注入、预算和评分仍由原公共实现负责。

`no_takeover` 强制禁用云端规划器，无需提供云端端点、模型或凭据文件；可用它验证纯本地链路。`takeover` 必须提供完整云端端点、模型和显式凭据文件。运行结果保存在 `results/`，由对应的 `summary.json` 和 `report.md` 记录。

```bash
python3 scripts/run_live_smoke.py --help
```

| 参数 | 用途 |
| --- | --- |
| `--bench-root` | 明确选定已支持 `handoff_v1` 的 Benchmark checkout，拒绝混入其它副本 |
| `--device`、`--adb`、`--appium-url`、`--baseline` | 空闲的独立设备及其已验收基线，不能与其它实验共享同一设备 |
| `--instances`、`--mode` | 1～3 个公开实例编号及模式，不扩展任务集或改写评分规则 |
| `--credentials-file`、`--planner-key-var`、`--local-key-var` | 从明确文件中选取密钥字段，不把密钥放入命令行，不改变全局环境，不向结果复制密钥 |
| `--planner-endpoint`、`--planner-model` | 完整云端 Chat Completions 请求地址和模型 ID；URL 逐字使用，不自动加后缀 |
| `--planner-disable-thinking`、`--planner-direct` | 显式选择兼容的生成选项，以及是否仅让规划器绕过环境代理 |
| `--local-base-url`、`--local-model` | 本地 OpenAI 兼容 SDK 基址及实际服务模型 ID |
| `--local-agent`、`--local-prompt-path` | 选择 `gui_owl`、`phonebuddy`、`qwen`、`deepseek`、`openai`、`claude`、`ui_voyager` 或 `holo`；PhoneBuddy 可显式指定固定官方提示词 |
| `--output` | 新建的本地结果目录；默认位于本项目 `results/` |

脚本先检查设备锁和 Appium 空闲状态，随后依次恢复原基线、调用原任务准备流程、进入原 Runner 与评分器、收集结果并封存当前环境生命周期。不会启动或重启模型服务、修改 `.env`、扩大决策预算或把恢复代码写进 Benchmark。

当前默认共享总额度为 20，可通过 `--max-decisions` 或 `RECOVERY_MAX_DECISIONS` 调整；GUI 动作、查证完成、模型完成／失败均使用同一运行时计步。云端分析最多两次且只在恢复准备阶段。HandoffBench 当前允许 1–200 的共享预算，超过 200 会在预算解析阶段拒绝。Calendar 的日期绑定和通用全库调度不在这个入口的支持范围内；目前任务准备适配器仅支持 `legacy_v1`／`apps_v1`。

结果保存摘要、源码指纹、准备／环境证据、原始 Benchmark 结果和 `recovery.json` 方法记录。功能检查关注是否发生有效接管、云端是否成功调用、决策计数是否一致、交付后是否零云端调用以及原评分链路是否可用；不要求 `task_success` 为真。没有触发接管、仅异常回退或评分不可用，不能写作恢复链路已验收。少量实例也不保证模型自然选择查证，必须根据真实规划记录说明走过的分支。

本项目的入口应明确导入本项目与所选 HandoffBench 版本，不复制 Benchmark Runner，不把工作目录或历史实验目录当成隐式依赖。模型服务和模拟器由现有运行环境提供。

PhoneBuddy 纯本地无接管冒烟示例（服务需预先监听 `8001`）：

```bash
PYTHONPATH=src \
python3 scripts/run_live_smoke.py \
  --bench-root /home/rich/MobileAgent/ResumeBench/HandoffBench \
  --baseline /home/rich/MobileAgent/ResumeBench/HandoffBench/.runtime/environment-baselines/full-worker8-v1.json \
  --device emulator-5574 \
  --adb /home/rich/MobileAgent/ResumeBench/HandoffBench/.runtime/android-sdk/platform-tools/adb \
  --appium-url http://127.0.0.1:4743 \
  --instances 14 --mode no_takeover \
  --local-agent phonebuddy \
  --local-base-url http://127.0.0.1:8001/v1 \
  --local-model PhoneBuddy-4B
```

该命令不调用云端 Planner；PhoneBuddy 的成绩应与 GUI-Owl 及 takeover 结果分开记录。
