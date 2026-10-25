# HandoffHarness

[English](README.md) | **简体中文**

面向用户接管场景的 Android GUI Agent 恢复辅助框架，帮助 Agent 在用户交还控制权后继续任务。

可搭配自己的 Planner、GUI Agent 与 [HandoffBench](https://github.com/Rich-King395/ResumeBench) 任务运行。仓库及 Python 包保留 `HandoffRecovery`、`handoff_recovery` 名称。

## 🧭 可以运行什么？

| 目标 | 入口 |
| --- | --- |
| 在 1–3 个实例上联调恢复流程 | `scripts/run_live_smoke.py` |
| 对指定任务或全量任务运行恢复实验 | `scripts/run_recovery_batch.py` |
| 运行无恢复基线，包含接管／无接管模式 | [HandoffBench](https://github.com/Rich-King395/ResumeBench) |

HandoffBench 当前提供 **18 个 App、200 个有效任务实例**。比较时应使用相同任务集、环境与 GUI 决策预算；建议起始配置保留模型原生终止方式，关闭条件更新和完成检查。

在新服务器部署或参与维护，请先阅读[协作者部署指南](docs/COLLABORATION.zh-CN.md)。

## 🚀 快速开始

先准备 HandoffBench、Android 环境和一个可用的 GUI Agent，再在**同一个 Python 环境**中安装 HandoffHarness：

```bash
git clone https://github.com/Rich-King395/HandoffRecovery.git
cd HandoffRecovery
python -m pip install -e '.[cloud-agents]'
python scripts/run_recovery_batch.py --help
```

Android 与模型客户端依赖由 HandoffBench 的安装环境提供；仅安装本包不能建立完整实验环境。

随后阅读[使用指南](docs/USER_GUIDE.zh-CN.md)，配置 Planner、指定 Benchmark 路径并运行首次恢复试验。

## 🤖 选择模型

| 角色 | 可选项 |
| --- | --- |
| Planner | 已配置的 OpenAI 兼容 Chat Completions 服务，或自定义 Planner 适配器 |
| 商业 GUI Agent | `qwen`、`deepseek`、`openai`、`claude` |
| 专用 GUI Agent | `phonebuddy`、`holo`、`gui_owl`、`ui_voyager`、`ui_venus`、`openmobile`、`mai_gym` |
| 通用本地视觉模型 | `qwen35`、`lfm25_vl` |

UI-Venus-2-9B（`ui_venus`）、OpenMobile-2-9B（`openmobile`）和 MAI-Gym-9B（`mai_gym`）已在两个项目中正式注册，保留各自原生协议。使用 native 终止，关闭条件更新与完成检查；OpenMobile 和 MAI-Gym 必须显式设置输出 token 上限。请配置自己的兼容模型端点，先做单任务 smoke，再进行全量实验。LFM2.5-VL 仍处于实验阶段。

## 🛠 二次开发

- 添加 Planner 或 GUI Agent 适配器。
- 固定任务集、模型和共享预算，比较不同恢复配置。
- 复用 HandoffBench 任务与独立评分进行实验。

扩展入口与离线检查见[开发与验证](docs/USER_GUIDE.zh-CN.md#开发与验证)。

## 文档导航

[使用指南](docs/USER_GUIDE.zh-CN.md) · [命令索引](scripts/README.md) · [公开分发边界](PUBLIC_RELEASE.md) · [HandoffBench](https://github.com/Rich-King395/ResumeBench)

[配套源码版本](docs/COMPATIBILITY.md)。
