# 项目文档

Agent 持续研究 Policy、采集、训练与评估，提高 A3 2×5、2×10 全量评测的准确率与通过率；维护者依据原始轨迹更新 Harness。常规 Harness 使用本地原生进程，远程 Controller 管理计算作业，日常升级继续使用原研究工作区。

## 使用指南 · `guides/`

- [环境配置](guides/setup.md)：Python 环境、模型服务、认证与部署参数。
- [启动与恢复](guides/usage.md)：首次运行、原地升级、夜间窗口与常用工具。
- [研究看板](guides/dashboard.md)：本地只读网页，查看最新运行、计划、记忆与技能。
- [维护与发布](guides/maintenance.md)：暂停、发布、人类反馈与输入更新。

## 机制与接口 · `reference/`

- [系统架构](reference/architecture.md)：Harness、Host、Controller 的职责与实验数据流。
- [目录职责](reference/storage-layout.md)：源码、工作区、轨迹、实验记录与展示网页。
- [Agent 输入](reference/agent-inputs.md)：三个 prompt 的分工、版本快照与 skills 发布。
- [上下文与长期记忆](reference/context-and-memory.md)：完整摘要、总 token 目标、原始历史检索与持久经验。
- [远程进程后端](reference/remote-process.md)：服务器队列、资源准入、路径、等待与恢复。
- [远程 Docker 后端](reference/remote-docker.md)：容器构建、隔离、Controller 协议与资产封存。
- [episode 执行器](reference/episode-executor.md)：并发、完成台账与结果复用。
- [运行遥测](reference/telemetry.md)：可观测状态、延迟与资源记录。
- [视觉诊断](reference/visual-diagnostics.md)：图像、同步轨迹与过程证据。

## Agent 提示词与方法

- [长期行为原则](../prompt/SOUL.md)、[执行约定](../prompt/execution.md)、[工作区说明](../prompt/workspace.md)保存在根目录 `prompt/`。
- [研究 skills](../skills/README.md)提供可按需检索的研究方法。

## 历史材料 · `history/`

[历史文档](history/README.md)集中保存设计、实施计划和审查记录。旧阶段提示词原文保存在根目录 [legacy_prompt/](../legacy_prompt/README.md)。第三方源码归属见 [third_party/README](../third_party/README.md)。
