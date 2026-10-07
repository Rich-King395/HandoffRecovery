# HandoffRecovery

面向用户接管后的 GUI 任务恢复方法：由云端模型判断当前任务状态、整理剩余目标，再由本地 GUI Agent 继续执行。

**当前阶段：控制器、规划器、本地适配和 HandoffBench 公共接口桥接已实现（2026-10-07）。** 124 项离线检查通过。GUI-Owl、PhoneBuddy，以及 HandoffBench 中的 Qwen、DeepSeek、OpenAI、Claude 共享视觉 Agent 均可通过统一 Executor 接口接入；共享云端适配器复用 HandoffBench 的请求、动作解析和坐标协议。真实云端恢复与查证回交仍按场景单独验收；不据小样本宣称恢复效果。运行参数和限制见 [脚本说明](scripts/README.md) 与 [实现说明](docs/IMPLEMENTATION.md)。

## 项目位置与职责

```text
MobileAgent/
├── HandoffRecovery/              # 独立的恢复方法项目（本项目）
├── ResumeBench/HandoffBench/     # 独立维护的 Benchmark
├── local-models/                 # 已有本地模型及其运行环境
└── shared-runtime/               # 已有 Android SDK 等公共运行环境
```

HandoffRecovery 负责恢复判断、剩余目标和本地执行适配。HandoffBench 负责环境准备、任务与接管调度、决策预算以及独立评分。本项目不复制 Benchmark 源码、任务库、评分器、模型权重或模拟器，也不改写其历史结果。

## 目录结构

```text
HandoffRecovery/
├── README.md
├── AGENTS.md                     # 后续开发约定
├── pyproject.toml                # Python 包与可选本地模型客户端依赖
├── .gitignore
├── .env.example                  # 无密钥的本机配置示例
├── docs/
│   ├── PLANNER_PROMPT.md         # 证据判断、输入与英文提示词
│   └── IMPLEMENTATION.md         # 已实现的接口、调用方式与接入限制
├── src/
│   └── handoff_recovery/
│       ├── core/                 # types.py 交接消息、coordinator.py 协作控制器
│       ├── planner/              # 公共输入、英文提示词、输出校验、客户端与适配器注册表
│       ├── executor/             # GUI-Owl/PhoneBuddy 原生协议、历史与适配器注册表
│       └── integrations/
│           └── handoffbench/     # 显式加载公共函数，适配 handoff_v1 协议
├── tests/
│   ├── unit/                     # 控制器、规划器与执行器的离线测试
│   ├── integration/              # 公共函数与真实 Runner 的离线集成检查
│   └── fixtures/                 # 可分享的最小测试素材
├── scripts/                      # 小规模联调和正式恢复批量入口
└── results/                      # 本项目的本地运行记录，默认不入库
```

核心流程、规划器 HTTP 客户端和离线测试仅需要 Python 3.10+ 标准库。GUI-Owl、PhoneBuddy 和共享视觉云端 Agent 的真实客户端需要显式选定 HandoffBench 路径及 `Pillow`；本地 OpenAI 兼容服务还需要 `openai`，云端 Agent 使用 HandoffBench 对应的 `api_method_*` 配置。PhoneBuddy 还需要已准备好的本地服务和官方提示词文件。本项目不自动启动或修改已有模型运行环境。

## 验证代码

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -m unittest discover -s tests -v
```

默认跳过需要指定 Benchmark 路径的兼容及 Runner 集成检查。额外运行这些离线检查（仍不调用服务或设备）：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
HANDOFFBENCH_ROOT=../ResumeBench/HandoffBench \
python3 -m unittest discover -s tests/integration -v
```

2026-10-07 验证：124 项离线测试通过，包括 PhoneBuddy、GUI-Owl 及共享云端 Executor 的请求、历史、坐标适配检查，以及公共函数／Runner 集成检查。覆盖初始即接管、接管前完成声明被延后、查证完成单独计步、预算边界，以及交付后不再调用云端。这不代表真实模型或设备验收通过。

代码接口和当前接入限制见 [实现说明](docs/IMPLEMENTATION.md)。客户端只接受显式配置；小规模联调脚本可通过 `--credentials-file` 从指定文件选择密钥字段，不修改全局环境、不复制密钥到结果。

## 少量真实调用联调

`scripts/run_live_smoke.py` 使用显式指定的 Benchmark 路径、独立设备、Appium 和基线，依次执行环境恢复、原任务准备、公共 Runner 和原评分器，并保存结果与环境生命周期收尾记录。一次仅接受 1～3 个不同实例，目的是检查恢复链路，不以任务成功为通过条件。

```bash
python3 scripts/run_live_smoke.py --help
```

实际运行还需要兼容的 Python 环境、空闲设备和可用模型服务。云端 `--planner-endpoint` 必须是完整请求 URL，脚本不会追加路径；本地 `--local-base-url` 则是 OpenAI 兼容 SDK 的基址。不要把 API Key 写进命令行。参数与结果说明见 [运行入口](scripts/README.md)。这不是全库正式评测入口。

## 当前恢复流程

- 云端规划器仍独立负责恢复判断；GUI Agent 可选择 GUI-Owl、PhoneBuddy 或 HandoffBench 已支持的 Qwen、DeepSeek、OpenAI、Claude。Coordinator 的规范化动作名称与 HandoffBench 完整动作集合一致，字段和值仍由 HandoffBench 宿主校验。更换执行器不改变恢复状态机、Planner 调用次数或 HandoffBench 评分规则。
- 接管结束后进入恢复准备阶段：云端判断状态，必要时向本地 Agent 下达查证任务，根据返回的可见证据整理剩余工作。原型暂定最多一轮查证、最多两次云端分析。
- 云端交付一个覆盖原任务全部剩余工作的目标后，由本地 Agent 独立执行到结束；执行中、遇阻后和结束时均不再调用云端。原始目标保持不变，云端输出区分已确认状态与未知信息。
- 云端失败或在查证上限内仍无法形成可靠建议时，记录原因并回退原生执行路径，不重置预算。
- 接管前后共享运行时配置的 Agent 决策额度，默认是 **20 次**，不重置、不额外增加 GUI 操作额度；云端分析次数、token 和耗时另记。批量入口可通过 `--max-decisions` 或 `RECOVERY_MAX_DECISIONS` 调整（HandoffBench 当前允许 1–200）。
- 暂不增加提前终止机制，保留 Agent 原生的完成／失败动作。
- 先用 2～3 个简单任务验证直接交付与查证后交付两条流程；后续再考虑扩大范围和对比实验。

控制器现已提供显式暂停／归还通知和独立的查证完成事件，每次 `step` 最多发起一次本地决策，动作需要宿主确认执行结果，所有阶段共享运行时预算。Coordinator 完整保留公开 `own_history` 供 Planner 和审计使用；GUI-Owl 与 PhoneBuddy 的 native context 按 `scope` 隔离，同一阶段内连续保留，切换阶段时重新开始。

HandoffBench 新增独立于本方法的可选公共协议 `handoff_v1`；`HandoffRecoveryAgent` 可传入支持该协议的 `run_task`。Runner 提供任务开始、控制权暂停／归还、决策额度和动作执行通知，并接收明确的查证完成事件。算法、提示词和模型调用仍全部在本项目，旧 Agent 未启用协议时保持原行为，历史结果不改写。

公开的归还通知增加了 Agent 可获得的信息。后续公平对比需要给相应基线提供相同的通知与观察条件，不能把新方法与未通知的历史结果直接解释为恢复算法的因果收益。

详见 [规划器输入与英文提示词](docs/PLANNER_PROMPT.md) 和 [实现说明](docs/IMPLEMENTATION.md)。结果目录只存方法自身的运行记录；任务是否成功仍由 HandoffBench 的原始任务判据决定。
