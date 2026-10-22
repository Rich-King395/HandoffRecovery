# Command index

> **Documentation preview:** this branch contains documentation only. Guides describe the forthcoming source release; current code is on [main](https://github.com/Rich-King395/HandoffRecovery/tree/main).

[English user guide](../docs/USER_GUIDE.md) · [中文使用指南](../docs/USER_GUIDE.zh-CN.md)

| Script | Use / 用途 |
| --- | --- |
| `run_recovery_batch.py` | Recovery experiments on selected stable task IDs or all active tasks / 按稳定任务 ID 或全量运行恢复实验 |
| `run_live_smoke.py` | Device smoke test on 1–3 public instances / 1–3 个公共实例的实机联调 |
| `run_qwen_protocol_smoke.py` | Planner protocol check; inspect `--help` before a real API call / Planner 协议检查；真实调用前先查看 `--help` |

Run commands from the repository root in the Python environment shared with HandoffBench. Use `--help` for current options. The user guides contain setup, complete run commands, result handling and development checks.

请在仓库根目录使用已安装 HandoffBench 依赖的 Python 环境运行。当前参数以 `--help` 为准；环境配置、完整运行命令、结果说明与开发检查见上方使用指南。
