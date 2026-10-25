# 协作者部署指南

[English](COLLABORATION.md) | **简体中文** · [首页](../README.zh-CN.md)

1. 按配套的 [Benchmark 协作者指南](https://github.com/Rich-King395/ResumeBench/blob/main/resumebench/docs/guides/COLLABORATION.zh-CN.md)，拉取两个仓库、安装依赖、准备 Android 和 GUI 模型。
2. 复现本源码快照时，两仓库使用 `collab-20261025` 标签；协作开发则使用经过配套检查的 `main` 版本。
3. 将本项目安装到 Benchmark 控制器环境，从 `.env.example` 创建自己的 `.env`。
4. 按[使用指南](USER_GUIDE.zh-CN.md)配置 Planner 并运行一次恢复任务。Benchmark 的 `--tasks 2`、恢复 batch 的 `--tasks 2001`、smoke 的 `--instances 2` 通过不同编号空间选择同一 Clock 实例。
5. 确认真实 `VALID` 接管、Planner 调用、GUI 恢复动作与独立终态评分后，再扩大实验。

起始对照配置：共享 30 次决策、isolated 历史、native 终止、condition_updates 关闭、条件权威 `model_claim`、host completion check 关闭、constraints/uncertain 注入关闭。这些是显式实验配置，不替换历史默认值。基线／恢复应保持 GUI 模型原生协议、端点参数及生成配置一致。

每组并行实验独立分配 emulator、Appium／systemPort、baseline、输出目录与本地模型端点；Planner 可能共享提供方额度，应保证容量并单列 API 错误。不能隐藏重试或补充 GUI 预算。

先执行使用指南中的 unit／integration 测试，再做设备 smoke。在开发分支提交 PR。凭据、实验结果、快照、权重和助手指导文件留本地；协作者自行建立并验证 baseline，不分发维护者的实验快照或结果。
