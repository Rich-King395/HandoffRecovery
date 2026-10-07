# 测试目录

测试使用标准库 `unittest`，默认不联系模型或设备。脚本化规划器、执行器和模拟宿主用于验证流程；它们不产生真实任务成绩。

- `unit/`：输入边界、云端输出解析、上下文与预算协作等离线检查。
- `integration/`：真实 Benchmark 公共函数及 Runner 的离线集成检查，模型和设备由替身提供，不启动服务或占用设备。
- `fixtures/`：可分享的最小离线素材，优先合成数据；不依赖 HandoffBench 历史实验目录，不放真实凭据或私人截图。

在项目根目录运行 `PYTHONPATH=src python3 -m unittest discover -s tests -v`。指定 `HANDOFFBENCH_ROOT` 才会启用真实 Benchmark 公共函数／Runner 检查；模型响应仍由替身提供。2026-10-07 全部 124 项离线测试通过。

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src \
HANDOFFBENCH_ROOT=../ResumeBench/HandoffBench \
python3 -m unittest discover -s tests -v
```

`test_runner_bridge.py` 验证没有接管时不调用云端、初始即接管时不虚构前截图、接管前完成声明被延后、查证完成只计一步、GUI-Owl 原生 `answer` 的作用范围、设备动作确认，以及第 20 步查证／原任务完成的不同处理。公共生命周期回调不调用模型，交付后云端调用为零，原任务评分不由恢复方法赋值。

`test_phonebuddy_executor.py` 还验证 PhoneBuddy 请求、响应、原生 tool-call 解析、动作映射和历史序列化错误被分别分类；分类记录不包含原始回复或凭据，也不触发内部重试。

真实设备联调使用 `scripts/run_live_smoke.py` 与确认空闲的模拟器，结果单独记录。功能联调与正式评测成绩分开，不以模拟模型、参考执行或人工强制分支代替真实 Agent 成绩。
