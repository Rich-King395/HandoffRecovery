# v0.1 实现与调用边界

状态：2026-10-07。控制器及公共 Runner 桥接已实现，离线检查覆盖运行时预算与适配器注册。GUI-Owl、PhoneBuddy 及 HandoffBench 共享视觉云端 Agent 执行器均可接入；真实云端恢复与查证回交仍需按场景验收。离线通过或单例完成不代表恢复效果。

## 组件接口

| 组件 | 代码 | 实际职责 |
| --- | --- | --- |
| 交接数据 | `core/types.py` | 原任务、PNG 快照、自身动作、查证／剩余目标、预算消息 |
| 协作控制器 | `core/coordinator.py` | 管理暂停、归还、准备、查证和最终执行，统一计步与调用次数 |
| 云端规划器 | `planner/` | 公共输入与图片编码、英文提示词、输出校验、单次 HTTP 客户端和可注册适配器 |
| 本地执行器 | `executor/` | 固定原始目标，当前消息附加阶段说明，保留各自原生历史与动作解析；通过可注册适配器接入不同 GUI Agent |
| Benchmark 加载 | `integrations/handoffbench/loader.py` | 从显式 checkout 路径导入公共辅助函数，拒绝混用副本 |
| 公共协议桥接 | `integrations/handoffbench/agent.py` | 通过 `handoff_v1` 接收宿主通知、核对额度、返回普通动作或查证完成，不读取私有状态 |

## 宿主与控制器的交接

1. 创建 `RecoveryCoordinator(task_id=..., original_goal=..., public_constraints=(...), planner=..., executor=...)`。控制器调用一次执行器的 `start_task`，此后阶段转换不再重置它。
2. 宿主提供普通 Agent 可见的 `PublicFrame`，调用 `step(frame)`。一次 `step` 最多调用一次本地模型；返回 `action` 时，宿主验证并执行动作，再调用 `acknowledge_action(success=...)`。
3. 动作尚未确认时不能进入下一步。失败会记录 `device_error`；动作成功也不代表任务成功。
4. 接管开始前显式调用 `pause_for_handoff()`。暂停期间 `step` 被拒绝。归还后调用 `return_control(fresh_frame)`，接管期间的观察不传入本控制器。若宿主将接管前的原任务完成声明延后以检查接管入口，桥接可显式重新打开该声明；仅允许原任务完成、仍有额度的情况，历史与计数不重置。
5. 下一次 `step` 进入云端准备。若要求查证，当次可以调用一次本地模型开始查证。查证中的原生 `done`／`fail` 返回给宿主的是 `verification_complete`／`verification_failed`，该次决策已经计数；同一次 `step` 不会再调用另一个本地决策或云端分析。
6. 宿主在查证事件后提供新的普通观察，再次调用 `step`，此时才进行第二次且最后一次云端分析。第二次仍请求查证会回退原生执行。
7. 目标交付后持续本地执行，`done`／`fail` 按原任务正常终止。云端不能因本地遇阻、完成或评分再次参与。

控制器覆盖完整任务生命周期，以便接管前决策也计入总预算。不支持中途构造一个新控制器并重新获得预算。总额度由宿主的 `TaskStart.max_decisions` 或 `RecoveryBudget.total_decisions` 在运行时提供，目前默认仍为 20。每次本地请求尝试均计数，包括失败请求和查证完成声明；终止边界随配置额度变化。

`report()` 返回阶段、事件、预算、云端耗时和 token、自身动作及图片元数据，不含图片 Base64 或凭据。`task_success` 固定为空；`task_complete` 只表示模型声明结束，不是成功评分。

## 观察与原生上下文

`PublicFrame` 保存不可变 PNG 字节，证据 ID 不能重用为另一幅观察。每次本地决策需要新的采集 ID；界面未变化时允许图像字节相同。它仅检查大小与 PNG 头部，不是完整图片解析器。

规划器收到接管前最后一次自身观察、归还后的首次观察、最近两张查证观察以及最新观察，按证据 ID 去重，同时收到整个任务的自身动作文字历史。这是原型图像选取策略，不保证多页查证的早期细节全部保留。

GUI-Owl 沿用原生系统提示词、四张阶段内历史图片加当前图片、全部合法自身输出，以及 Benchmark 公共坐标映射和完成动作解析。阶段说明仅加入当前 user 消息，`goal` 保持原始任务；进入 `verification`、`execution` 或 `fallback` 时会切换 native context，接管前的对话历史不会带入恢复阶段。

公开输入契约不是文件系统沙箱。宿主仍负责只提供普通 Agent 观察，不传入接管私有截图、数据库或评分证据。

## 显式客户端配置

云端使用 `PlannerConfig(endpoint=..., model=..., api_key=...)` 和 `OpenAICompatiblePlanner(config)`。`endpoint` 是完整 Chat Completions 请求地址，逐字使用，不拼接后缀。`planner.registry` 提供 `register_planner`、`create_planner` 和 `module:factory` 动态加载；自定义工厂接收显式 `PlannerConfig` 并返回实现 `PlannerProtocol` 的对象。客户端不读取环境或 Benchmark 的 `.env`；`run_live_smoke.py` 是显式凭据装配入口，读取 `--credentials-file` 中选定的密钥字段，不修改全局环境或写出密钥。

每次 `plan` 最多一个 HTTP 请求，不自动重试或跟随重定向；默认输出上限 2048 token、响应上限 1 MiB、网络超时 60 秒。该超时不是独立进程强制的端到端总时限。默认沿用宿主环境代理，可显式设置 `use_environment_proxy=False`。错误不输出请求头、响应原文或密钥。

本地可使用 `GUIOwlExecutor(...)`、`PhoneBuddyExecutor(...)`，或使用 `CloudExecutor(bench_path, CloudExecutorConfiguration(...))` 接入 HandoffBench 的 `qwen`、`deepseek`、`openai`、`claude` 共享视觉 Agent。后四种适配器复用 HandoffBench 的 `api_method_*` 请求、JSON 动作解析和坐标映射，只把恢复阶段上下文追加到当前任务目标中；它们不会嵌套 HandoffBench 的完整 Agent 生命周期。`executor.registry` 提供 `ExecutorSpec`、`register_executor`、`create_executor` 和 `module:factory` 动态加载；自定义工厂接收 `(bench_path, ExecutorSpec)` 并返回实现 `ExecutorProtocol` 的对象。自定义适配器可提供 `validate_action`，由公共桥接使用其规范化动作校验。每次决策最多一个请求，SDK 重试关闭，Executor 的原生输出错误会分类记录为 `request_error`、`response_error`、`native_parse_error`、`action_mapping_error` 或 `integration_error`，同时保留异常类名；不记录原始模型回复，也不自动重试。错误分类不会改变共享决策计数。

`ExecutorProtocol` 的必需方法是 `start_task(task_id, original_goal)` 和 `decide(frame, instruction)`；注册表和 Coordinator 会在任务开始前检查这两个方法。Coordinator 的动作名称集合与 HandoffBench `validate_action` 完整对齐，包括 `double_tap`、`drag` 和 `close_app`；字段、坐标、持续时间和包名的完整校验仍由宿主的 `validate_action` 执行。`validate_action`、`descriptor` 和 `requires_completion_check` 是可选能力，但一旦适配器提供就会检查其可调用性、JSON 可序列化性和布尔类型。`descriptor` 只能包含模型、协议、坐标空间等非敏感实验元数据，不得包含 API Key 或原始响应。

## HandoffBench 公共桥接

`HandoffRecoveryAgent` 可传入实现了 `handoff_v1` 的 `run_task`；控制器本身不直接替代 Benchmark Agent。构造示例：

```python
from handoff_recovery.integrations.handoffbench import HandoffRecoveryAgent

agent = HandoffRecoveryAgent(
    bench_path=benchmark_checkout,
    planner=cloud_planner,
    executor=local_gui_owl_executor,
)
```

公开消息由 Benchmark 构造，只包含公开任务与额度、普通 Agent 观察及动作是否执行成功：

| 接口 | 行为 |
| --- | --- |
| `on_task_start(TaskStart)` | 创建一次控制器和本地任务上下文，保留原任务及可见约束 |
| `on_control_event(ControlEvent)` | 暂停观察；归还时接收新采集的普通截图，下一次正常决策才调用模型 |
| `on_decision_start(DecisionContext)` | 对照宿主已用额度和决策编号，不独立追加预算 |
| `decide(observation, goal, history)` | 最多一次本地模型决策；准备阶段可另有一次云端分析 |
| `on_action_result(ActionResult)` | 确认宿主实际执行结果，不视为任务评分 |
| `SubtaskCompletion(action, scope="verification")` | 一次已计数的查证 `done/fail`；不操作设备、不结束原任务、不触发成功评分 |

生命周期回调不调用模型、采集设备或执行动作。接管发生在首个决策前时，没有虚构的接管前截图；接管前最后一次截图始终来自自身正常观察。查证完成后，Runner 再采集一张普通截图进入下一次决策，不吞掉完成声明后在内部追加请求。

当前 HandoffRecovery 控制器接受运行时总额度，默认值为 20；桥接会核对宿主提供的共享额度。HandoffBench 当前 checkout 仍有自己的 20 步上限，因此要真实运行大于 20 的实验，还需同步放宽该 Benchmark 版本的预算校验；这不影响 HandoffRecovery 核心和离线测试的动态预算支持。Runner 是唯一的权威计数和执行者，控制器逐次核对计数。

`agent.report()` 是方法诊断，仍不分配成功分数。实际任务与恢复成绩取 `run_task` 结果。原生 Agent 不声明 `public_protocol_version="handoff_v1"` 时不收到这些通知，旧行为保持不变。

## 联调与比较边界

`scripts/run_live_smoke.py` 选择 1～3 个实例，复用原基线恢复、任务准备、设备锁、公共 Runner、独立评分和环境收尾；记录源码摘要、云端调用、方法事件与原始结果。它是功能联调入口，不是正式全库批测器，不要求模型完成任务。任务未接管、云端失败而回退或评分链路异常必须记录，不能把单纯本地执行称为已跑通云端恢复。

当前离线集成检查覆盖了原生 GUI-Owl `answer`、查证后交付、阶段 context 隔离、接管前完成声明延后、动作失败确认和预算边界。真实模型选择、服务可用性与设备链路仍需单独验收，自动全库批测、真机支持和效果比较不在本轮范围内。

公开的归还通知改变了信息条件。今后对比恢复方法与基线，应让双方获得相同通知与正常观察、采用相同总额度，并分别记录额外云端成本。未获得通知的历史结果可作参考，不能直接作为严格的因果对照。

正式效果实验前还需对齐异常策略：当前独立执行器遇到无法解析的原生输出会结束为 `agent_error`，既有 GUI-Owl 基线可将该次计为无效决策并继续。此次功能联调不证明两者在所有异常路径上的行为等价，不能据此直接做公平的成功率比较。
