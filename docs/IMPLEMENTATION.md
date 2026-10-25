# v0.1 实现与调用边界

## PhoneBuddy单次调用适配继续探索（2026-10-21）

用户选择保留单次原生调用、不引入独立条件判定。新增24次独立诊断推理：18次针对原生Thought、finish.text及XML承载，6次针对同一generate的前缀／尾部结构。8次提示输出出现条件字段名，但原生调用格式无效；所有组合仍未通过验收。仅强制尾部起始框架也不能保证元数据闭合，直接接入会引入合法动作被残缺尾部拖累的风险，因此未实施生产修复、未放宽parser或自动补声明。

## PhoneBuddy条件声明专项验证（2026-10-21）

当前PhoneBuddy-4B真实 `condition_updates` 输出未通过验收：4张正常Agent公开截图上的7种格式候选共28次推理均无声明，另2次真实生产Executor／SDK／HTTP探针确认扩展已注入请求，但仍无声明。原生processor渲染／tokenization的合成检查保留元数据，旧日志也未发现被解析器漏掉的声明。证据更符合模型对附加输出要求遵循不足，不将其推断为所有可能方案均无效。

Worker4／独立8010服务的一条Contacts公共3实机任务自然接管VALID，Planner一次EXECUTE，交付后云端0次；共享30步中前缀2＋恢复8＝10，业务与恢复评分成功，但8次missing、5条件全pending。显式enabled＋native／model_claim／isolated，其余注入及completion_check关闭。不能把任务成功当作条件机制有效，或据此启动已验证的conditions门槛组。

## PhoneBuddy三路并发与条件开关实测（2026-10-21）

三实例使用同revision／BF16／SDPA，分别8002／8009／8010；两轮同步推理6/6成功，真实Clock公共2三组均自然接管VALID、Planner一次EXECUTE，交付后云端0次，无服务冲突／OOM／超时。A更新关闭＋native使用9步；B更新开启＋native使用8步、3次missing；C更新开启＋conditions使用30步、25次missing及23次done_rejected。三个任务评分均可用但业务失败，B／C均无条件声明，不能据开关开启宣称模型会使用元数据或条件机制有效。缺省行为、原生协议、共享预算及独立评分均未改。

## 通用本地 VLM 接入（2026-10-20）

新增 `qwen35` 与 `lfm25_vl`，分别对应 Qwen3.5-2B、LFM2.5-VL-3B。它们属于通用 VLM，不复制专用 GUI Agent schema：复用 Benchmark 通用 GUI JSON 策略和原图整数像素坐标，保留服务端官方 chat template／视觉处理与各自生成参数。统一 ExecutorSpec／registry 复用 CloudExecutor 和 load_cloud_bindings，类名中的 cloud 只沿用共享执行层，不改变模型部署位置。

GUI 请求配置以 Benchmark 的 QWEN35_*／LFM25_VL_* 为准，smoke／batch 记录实际模型、地址、输出上限和来源。历史、Planner、Coordinator、completion_policy 默认 conditions、shared budget、Last action feedback 移除策略及独立评分均保持。新入口使用现有 JSON condition_updates，显式开关与 native 完成独立；首轮推荐 native＋disabled。

新入口的非法／空／截断／拒绝／意外工具输出不产生设备动作、计一步后继续，失败输出不追加 Executor 历史；分类 native_parse_error。HTTP、超时和响应结构错误分类 request_error 并终止；HTTP 和模型输出均不重试。此处只为新入口接通既有可恢复异常属性，不修改旧商业模型的错误处理，也不修改 Coordinator。

离线基线210项通过；新增6个集成方法后，完整回归216项全部通过。使用真实 Benchmark HTTP构造／解析／映射和 Runner，模型与设备为替身；覆盖基线原始请求对照、两模式、作用域历史、fallback、末步完成、非法输出预算耗尽、条件声明回执及脚本配置。新增 Bench 测试和既有调用方的验收范围见[接入指南](https://github.com/Rich-King395/ResumeBench/blob/main/resumebench/docs/agents/GENERAL_LOCAL_VLM.md)。

Benchmark 的定向70项仍有基线相同的1失败／2报错子项；补充旧批测检查另有4方法9失败子项，涉及旧预算与场景断言。本轮不修改这些测试和业务规则；它们与已修复的 Recovery 9项集成失败是不同集合。

宿主8007／8008无新服务，local-models索引无两模型，GPU约余4.7／5.5 GiB；ADB与目标Appium可用不等同模型就绪。本轮不下载权重、不启停既有服务、不升级依赖、不启动真实任务或全库批次；仅完成离线接入，真实模型与端侧推理待验。

## 既有9项集成失败修复（2026-10-20）

完整离线基线为209项、200通过／9失败。本轮只修改三份集成测试及验收记录，两个仓库共136个生产 Python 文件的前后 SHA-256 一致；不改变 Coordinator、Executor、模型协议、默认配置、预算或评分。

- Runner 的5个 conditions 用例补齐有效的 `condition_updates` 模拟输出，保留原有计步、调用次数、通知、隔离及评分断言；原生 SDK 用例显式使用 native，扩为 VERIFY／EXECUTE 各两次决策，验证阶段内原生历史保留、阶段之间隔离、原生终止不发送设备动作且条件仍 pending。
- 两个原生辅助函数用例按当前分层任务文本验证完整请求布局，覆盖阶段切换、四张历史图窗口与旧动作摘要；非法输出在同一作用域中不追加历史，切回 original 时恢复原始快照，同时保留解析／映射错误分类和无重试检查。
- 共享商业策略用例保留非恢复 prompt 哈希对照，验证状态块是唯一条件列表、默认不注入 Planner constraints、恢复初始历史为空，后续只保留该恢复阶段的公开动作。
- 新增 Runner 检查：默认 conditions 拒绝无条件声明的 done，该次仍计步；随后有效声明可结束，但独立评分仍可失败，不把声明当作成功证据。

三份定向测试共20项通过；完整 Recovery 回归210项全部通过，无跳过。命令（从 `HandoffRecovery/` 运行）：

```bash
PYTHONPATH=src HANDOFFBENCH_ROOT=/path/to/project \
  ../ResumeBench/HandoffBench/.runtime/appium-venv/bin/python \
  -m unittest discover -s tests -p 'test_*.py' -v
```

未运行真实模型或设备，不据离线通过推断恢复收益或所有行为绝对不变；也未重跑 HandoffBench 独立测试，其此前记录的 Qwen／Claude 解析失败不在本轮范围。下文各批次的9项／10项失败是历史基线，不代表当前 Recovery 回归仍失败。

## condition_updates 独立开关（2026-10-20）

`run_live_smoke.py` 与 `run_recovery_batch.py` 新增 `--condition-updates enabled|disabled`，独立于 `--completion-policy native|conditions` 和 `--condition-update-policy host|model_claim`。开关沿公共桥接、Coordinator、LocalInstruction 传给 Executor。未指定时保留旧调用行为：专用模型不启用新增元数据通道，商业模型 conditions 模式沿用原有 JSON 条件声明；新实验应显式指定。

专用模型开启时只在恢复 EXECUTE 中追加版本化的输出格式说明。GUI-Owl／PhoneBuddy／UI-Voyager 的原生动作后可附加单独的 `<condition_updates>[...]</condition_updates>` 区块；Holo 同样支持 Qwen XML 后缀，或在原生结构化 tool_calls 之外通过 assistant content 提供区块。工具参数、坐标、图片处理和生成参数不变；这是可选的 Harness 输出扩展，不宣称开启时 prompt 与官方完全相同。原始 prompt 资产及默认请求不改写。关闭时不追加格式说明、不启用元数据分离，继续原有严格 native parser。商业模型开启时沿用 JSON `condition_updates` 字段，也可与 native 终止策略组合。

开启后按完整条件列表显示固定 c1…cN，空数组／缺省不推进状态。格式正确的元数据经现有条件校验器检查；未知或重复 ID、空 evidence、错误 status 等只拒绝条件声明，不丢弃可独立解析的合法动作。格式截断、重复区块、区块后额外动作等无法可靠分离的响应按已有无效决策路径计步，不重试。字段内容依据当前公开截图，不提前声明本轮动作的预计效果。普通动作的声明在成功执行回执后应用；done 同包声明先应用，再检查可选的 conditions 门槛。原有 satisfied 单调语义保留，模型声明不等于独立任务评分。

元数据从原生回复分离后才进入原 parser／mapper；原生历史保存清理后的原生内容。没有新增模型调用、设备动作或独立预算，VERIFY／原任务／fallback 不开启此通道，`Last action feedback` 继续不进入 GUI prompt。

推荐消融：A 为 `--condition-updates disabled --completion-policy native`；B 为 `--condition-updates enabled --completion-policy native`；可选 C 为 `--condition-updates enabled --completion-policy conditions`。三组均显式使用 `--condition-update-policy model_claim`，其余任务、Planner、历史、预算和生成设置一致。CLI 显式关闭更新却启用 conditions＋model_claim 且无 completion_check 时会提前拒绝，避免无条件状态来源的运行；未指定开关的旧默认行为不因此改变。

summary／报告记录有效开关、元数据协议和原生格式扩展 SHA-256；Recovery 记录 received／missing／rejected／ignored 及声明条件数。batch 的 summary 保留这些计数、最终条件数与完成动作拒绝数，summary retention 后仍可分析。原生 Executor descriptor 记录扩展协议与格式哈希。只记录可公开的配置与统计，不记录真实原始回复或私有评分证据。

本批离线结果：Recovery 从197项188通过／9项既有失败变为209项200通过／同9项失败；Benchmark 相关8模块从138项变为140项，仍为同1项失败、2个子用例错误（既有 Qwen／Claude 解析预期）。新增14个测试方法通过，覆盖四类 native parser、SDK 消息与参数、结构化 Holo、末步 Runner 桥接、开关及异常。32组任务文本和8组原生消息布局对照保持兼容，旧测试函数与固定 prompt 资产未改写。Recovery 使用项目 Python、`PYTHONPATH=src`、`HANDOFFBENCH_ROOT` 运行 `-m unittest discover -s tests -p 'test_*.py' -v`；Benchmark 使用 `tests.agents.test_resumebench_{cloud_agents,gui_owl,phonebuddy,ui_voyager,ui_voyager_history,holo,holo_transport,native_json}` 对应8个 unittest 模块。

本批尚未调用真实模型或设备。Holo 实际服务是否稳定保留混合 content／tool_calls、各模型声明使用率和恢复收益仍须先做单任务 smoke，再进行批量消融。旧实机结果不改写。

## GUI prompt 的动作回执边界（2026-10-20）

按用户要求，暂时移除 GUI prompt 中的 `Last action feedback`。商业模型与四类专用模型统一适用，覆盖 conditions／native、VERIFY／EXECUTE 及最终只读确认；回执不再通过任务文本绕过 native history 的阶段隔离。Coordinator、LocalInstruction 及内部诊断仍保留原有回执，原生历史、Planner、条件更新、终止方式、共享预算和独立评分不变。无需新增开关。下文同日早期接入说明及已有实机报告中的回执注入属于此前版本，历史结果不改写；移除后的模型表现尚未实测。

本批离线验证：Recovery 从195项186通过／9项既有失败变为197项188通过／同9项失败；Benchmark 商业适配器从35项变为36项，仍有同1项失败、2个子用例错误（既有 Qwen／Claude 响应解析预期），没有新增失败。新增3个测试方法通过，覆盖反馈不进入提示、原生历史保留、conditions／native 和各恢复阶段；28组修改前后文本对照只移除了回执区块。未调用真实模型或设备。

## 专用模型原生恢复接入（2026-10-20）

本阶段复用已有 `completion_policy="native"`，暂不增加本地模型的 `condition_updates` 通道。原始任务与恢复目标仍可见；条件未声明时保持 pending，不阻挡原生终止，也不由终止推导成功。旧默认 conditions、商业模型条件更新、Planner 策略及独立评分保持。PhoneBuddy 在 native 下也严格拒绝 finish 的 completion_check 扩展，与基线一致；旧 conditions 模式的检查保留。

| Executor | 继续复用的 Benchmark 协议 | 原生终止 |
| --- | --- | --- |
| GUI-Owl 2B／4B／8B | mobile_use、0–1000坐标、4张历史图＋当前图 | answer、terminate(success/failure) |
| PhoneBuddy | JSON／Qwen XML、嵌套points、0–1000坐标、全部自身Thought/Action | finish |
| UI-Voyager | mobile_use／既有本地mobile_drag、0–999坐标、最多30条原生Action摘要 | terminate(success/failure)；操作任务仍拒绝answer |
| Holo | 函数调用／Qwen XML、既有Android工具桥接、0–1000坐标、3图与自身正文／工具历史 | answer(content)；无工具正文计步、不终止 |

四类 Executor 仅将对应基线同样按无效决策处理的 parser／mapper `ValueError`／`OverflowError` 标为可继续。直接调用仍抛出原有分类异常；Coordinator 记录 invalid_decision 及错误分类，桥接返回无效决策给 Runner，计一次共享预算后等待下一次正常决策，不内部重试、不补设备动作、不追加非法原生历史。请求、集成、历史序列化及其他异常继续终止并分类。UI-Voyager 的 Thought／Action 元数据也与基线分别提取。 native 模式下四类模型使用相同的原任务／恢复目标／条件状态／公共动作回执文本；GUI-Owl、PhoneBuddy 补齐此前缺少的回执与状态，条件仍只作诊断，不新增模型输出字段。该差异由修改前失败、修改后通过的跨模型输入对照测试确认；conditions 旧行为保持。

普通 VERIFY 完成恰好占用最后一步时，桥接仍返回 SubtaskCompletion，Runner 以预算耗尽结束，不将查证完成误当作原任务 done。COMPLETE 的最终只读确认和 EXECUTE／原始阶段正常完成保持。

`registry.load_local_configuration` 统一加载四类原生配置；UI-Voyager／Holo 补齐 configuration_source，registry 与 module:factory 入口一致。正式 batch 默认四类均从 Benchmark 校验器读取 model、endpoint、token、timeout，PhoneBuddy 还读取 prompt 路径。explicit_executor_spec 保留给明确覆盖和 mock。smoke 原有显式配置默认保留，正式联调须显式传 `--configuration-source handoffbench_provider_validate_configuration`；此时 Benchmark 配置优先于 Recovery 同名覆盖，summary 记录实际原生配置。

四类 registry 原生工厂暂不支持 constraints／uncertain 注入，打开时在恢复／准备前报错；GUI-Owl／UI-Voyager／Holo 也拒绝未支持的 completion_check，避免静默忽略。默认均关闭，PhoneBuddy 原有 conditions 检查入口保留。

本批离线基线：Benchmark 定向107项通过；Recovery 180项、170通过／10失败。修改后195项、186通过／9项既有失败，无新增失败；原有最后一步 VERIFY 桥接失败由生产代码修复，原测试未改预期。新增15个方法覆盖四类真实 parser／Runner 的非法输出续做、预算耗尽、VERIFY／最终确认／fallback、原始阶段 SDK 请求与原生历史对照、配置及脚本传递。剩余9项为两条旧历史／注入预期、六条默认 conditions 下未提供条件声明的旧 Runner 用例、一条共享云端旧调用预期，仍待独立处理；不能据通过子集宣称完整验收。

## 显式原生完成模式（2026-10-20）

`RecoveryCoordinator`和`HandoffRecoveryAgent`新增`completion_policy="native"`；smoke与batch入口使用`--completion-policy native`。默认仍为`conditions`，不改变既有实验的条件门槛。该策略由Coordinator统一传给LocalInstruction，与模型种类无关；模型原始动作schema、坐标、图片及history策略不因此切换。

native下，模型仍收到原任务、恢复目标及可观察完成条件，使用各自已有的finish／answer／terminate或商业done结束。execution不再因pending诊断拒绝done，也不要求condition_updates／completion_check。未提供的逐项证据保持pending，不能由完成声明推导为satisfied；方法报告不赋予任务成功分。VERIFY的原生完成仍只是一次计步的子任务完成，后续重新规划、COMPLETE最终确认、fallback、失败、共享预算和交付后禁止Planner调用的规则保持。

native与`--enable-completion-check`冲突并在设备启动前拒绝；编程接口同样校验。两类脚本summary、batch报告及Coordinator诊断记录completion_policy。旧报告缺少字段按历史conditions理解，不回写旧结果。对比实验应在所有参评模型上显式使用相同模式，不混合两种策略作为同条件结果。

GUI-Owl和PhoneBuddy的Recovery专有容错已移除：两边对同一原生输出执行同一Benchmark mapper。严格拒绝像素越界坐标、扁平points和未知字段，不追加调用或修复模型动作。原native system prompt不改，图片与分阶段历史继续由各自适配器处理。该修改不意味着本地模型其他开关已全部接通，也不解决已有10项集成测试失败。

```bash
# 在既有、已校验的运行命令上显式追加；不单独执行本段。
--completion-policy native --disable-completion-check \
--recovery-history-mode isolated \
--recovery-constraints disabled --recovery-uncertain disabled \
--condition-update-policy model_claim
```

四类本地适配器通过真实parser、公共Runner和模拟设备完成原生终止到独立评分的离线验证；成功与失败两种评分均保留，VERIFY单独计步。四类商业模型的native提示、既有done schema及非恢复prompt哈希通过离线检查。未运行真实Planner／模型native恢复，不把上述检查当作模型成绩。

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
5. 下一次 `step` 进入云端准备。若 `completion_status=COMPLETE`，当次安排一次最终只读确认；确认 `done` 直接结束恢复并交给宿主评分，确认 `fail` 才允许下一次云端分析，且下一次只能选择 `EXECUTE` 或 `FALLBACK`。普通 `VERIFY` 仍将 `done`／`fail` 返回给宿主为 `verification_complete`／`verification_failed`，该次决策已经计数。
6. 普通查证完成后，宿主提供新的普通观察并再次调用 `step`，进行最后一次云端分析。查证失败会通过 `verification_failed` 标记传给 Planner，禁止再次请求 `VERIFY`。
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

本地可使用 `GUIOwlExecutor(...)`、`PhoneBuddyExecutor(...)`，或使用 `CloudExecutor(bench_path, CloudExecutorConfiguration(...))` 接入 HandoffBench 的 Agent。GUI-Owl 和 PhoneBuddy 的正式请求路径现在统一通过 loader 暴露的 HandoffBench `api_method_*.request_action` 入口；Recovery 只提供原始目标、恢复 scope 下的任务字段和本地 native history，模型请求、图像处理、消息布局、传输参数和响应提取由 HandoffBench 负责。`VERIFY`/`EXECUTE` 会把 Planner 的子任务作为 prompt 的 `Task`，并把 Planner 的约束和完成条件写入原有完成检查句；不会追加一段 JSON recovery context。`original` 与 `fallback` 保持原任务 prompt 不变。各模型仍保留自己的 native parser、坐标映射、`finish`／`completion_check` 和 history serialization，不强制转换为商业模型协议。注入式 `client_factory` 仅作为离线测试替身保留，不是正式运行路径。`executor.registry` 提供 `ExecutorSpec`、`register_executor`、`create_executor` 和 `module:factory` 动态加载；自定义工厂接收 `(bench_path, ExecutorSpec)` 并返回实现 `ExecutorProtocol` 的对象。自定义适配器可提供 `validate_action`，由公共桥接使用其规范化动作校验。每次决策最多一个请求，SDK 重试关闭，Executor 的原生输出错误会分类记录为 `request_error`、`response_error`、`native_parse_error`、`action_mapping_error` 或 `integration_error`，同时保留异常类名；不记录原始模型回复，也不自动重试。错误分类不会改变共享决策计数。
原生协议边界固定记录在 Executor descriptor：GUI-Owl 使用 `gui_owl_mobile_use_v1`，PhoneBuddy 使用 `phonebuddy_tool_call_v1`。Recovery 不把它们改写成共享模型输出协议；只在 native parser 成功后，调用 HandoffBench 的 mapper 转为宿主执行动作。`finish`、reasoning、`completion_check`、坐标空间和 native history 均由对应模型 adapter 保持。

Native history 由 Recovery host 的 `recovery_history_v3` 统一管理：原始 scope 保留原生历史；默认进入 verification 或 execution 时先保存前一 scope，再以空 history 开始，随后只追加本 scope 成功解析的输出；fallback 明确恢复 original snapshot。显式 A/B 模式 `recovery_history_mode=original_snapshot` 才会在恢复入口继承接管前的 original snapshot。恢复上下文不进入 native history。各 Executor 的 descriptor 都标记 `history_owner=handoff_recovery_host_policy`，模型 adapter 不得自行切换或恢复 history。

`progress` 保留为恢复方向提示，但 host 会在 Planner 解析、Coordinator 保存和 LocalInstruction 构造时重复执行有损清洗：每类最多 8 条、每条最多 160 个字符，只允许短的原子事实；第一人称、推理解释、猜测词和操作建议会被丢弃。`confirmed` 中带有未确认语义的条目会移到 `uncertain`，不会原样注入 GUI Agent prompt。该摘要不构成完成判断，最新截图和原任务完成条件仍然具有权威性。

模型配置来源也固定记录：正式批处理将 GUI-Owl、PhoneBuddy 以及共享云端模型的 model、base URL、API key、prompt 路径、采样和 token 默认值交给 HandoffBench 对应 `api_method_*.validate_configuration()` 校验；Recovery 的 `.env` 只选择 agent、Planner、设备和实验控制项。ExecutorSpec 的 `configuration_source=handoffbench_provider_validate_configuration` 会在注册表边界重新加载该配置，避免命令行或 Recovery 默认值覆盖 baseline。显式 `explicit_executor_spec` 仅保留给离线 mock 和迁移测试。

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
| `SubtaskCompletion(action, scope="verification")` | 一次非最终查证的 `done/fail`；不操作设备、不结束原任务、不触发成功评分。`COMPLETE` 分支的最终确认由普通终止动作结束并进入宿主评分 |

生命周期回调不调用模型、采集设备或执行动作。接管发生在首个决策前时，没有虚构的接管前截图；接管前最后一次截图始终来自自身正常观察。查证完成后，Runner 再采集一张普通截图进入下一次决策，不吞掉完成声明后在内部追加请求。

HandoffRecovery 与当前 HandoffBench 均接受任意正整数运行时总额度，默认 30，不再设固定 20／200 步上限。smoke／batch 使用 `--max-decisions N`，batch 省略该参数时读取 `RECOVERY_MAX_DECISIONS`，仍未设置时使用默认值。参考执行入口也支持相同参数。Runner 是唯一的权威计数和执行者，桥接及控制器逐次核对配置总额、已用和剩余额度；Planner 请求携带实际总额。用户接管操作在暂停期间单独计数，不消耗或重置 GUI 额度；接管前缀、恢复 VERIFY／EXECUTE／fallback、无效模型决策与完成／失败声明共用这一额度。旧阶段字段仅兼容，不产生阶段上限；历史结果和任务 fixture 的预算元数据不追改，各阶段墙钟超时保持。

2026-10-20 预算离线验收：Recovery 221项全套通过（修改前216项通过），Benchmark 112项定向通过；真实Runner联调覆盖3步额度内执行6次用户操作，以及250步共享前缀、VERIFY、EXECUTE、Planner实际余额与独立评分。预算验证不需要模型服务或设备；未据此宣称实机或Benchmark全套验收。完整目录CLI测试曾超过原20秒加载时限，小型真实配置CLI通过，超时未放宽。Recovery回归命令：在本仓库运行 `PYTHONPATH=src HANDOFFBENCH_ROOT=../ResumeBench/HandoffBench ../ResumeBench/HandoffBench/.runtime/appium-venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v`。

`agent.report()` 是方法诊断，仍不分配成功分数。实际任务与恢复成绩取 `run_task` 结果。原生 Agent 不声明 `public_protocol_version="handoff_v1"` 时不收到这些通知，旧行为保持不变。

## 联调与比较边界

`scripts/run_live_smoke.py` 选择 1～3 个实例，复用原基线恢复、任务准备、设备锁、公共 Runner、独立评分和环境收尾；记录源码摘要、云端调用、方法事件与原始结果。它是功能联调入口，不是正式全库批测器，不要求模型完成任务。任务未接管、云端失败而回退或评分链路异常必须记录，不能把单纯本地执行称为已跑通云端恢复。

当前离线集成检查覆盖了原生 GUI-Owl `answer`、查证后交付、阶段 context 隔离、接管前完成声明延后、动作失败确认和预算边界。真实模型选择、服务可用性与设备链路仍需单独验收，自动全库批测、真机支持和效果比较不在本轮范围内。

公开的归还通知改变了信息条件。今后对比恢复方法与基线，应让双方获得相同通知与正常观察、采用相同总额度，并分别记录额外云端成本。未获得通知的历史结果可作参考，不能直接作为严格的因果对照。

错误结果使用统一的两级协议：`error_class` 区分 `gui_agent_error`、`recovery_control_error`、`device_error`、`environment_error` 和 `lifecycle_error`，`error_category` 保留请求、原生解析、动作映射、Planner 校验、设备执行等具体原因。GUI Agent 协议错误不会再被归入恢复策略失败；Host 生命周期错误、Appium/ADB 环境错误也不会伪装成 GUI Agent 错误。批量报告会同时汇总这些类别，便于剔除流程错误后进行公平比较。
