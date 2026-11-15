# 项目文档

Agent 自主研究 Policy、采集、训练与评估；维护者依据原始轨迹更新 Harness。常规 Harness 使用本地原生进程，远程 Controller 管理计算作业，日常升级继续使用原研究工作区。

## 安装、运行与维护

- [环境配置](setup.md)：Python 环境、模型服务、认证与部署参数。
- [使用教程](tutorial.md)：首次 published 启动、恢复、原地发布、夜间窗口与常用工具。
- [系统架构](architecture.md)：Host、Harness、Controller、身份与数据流。
- [目录职责](project-structure.md)：发布源码、研究工作区、原始轨迹与升级档案。
- [操作者监督](operator-supervision.md)：维护发布、人类反馈与输入更新。
- [Agent 输入版本](agent-inputs.md)：目标、协议、skills 的完整快照与版本维护。

## Harness 能力

- [上下文与长期记忆](context-reliability.md)：完整摘要、总 token 目标、原始历史检索与持久经验。
- [单 Agent 研究流程](research-orchestration.md)：独立作业并行、研究记录与维护职责。
- [执行接口](../agent-inputs/shared/execution.md)：具体工具参数与 Host API。
- [GPU 与操作遥测](operational-telemetry.md)：可观测状态、延迟与资源记录。
- [视觉诊断](visual-diagnostics.md)：视频、图像与过程证据检查。

## 远程执行与科研输入

- [远程进程后端](process-backend.md)：服务器队列、资源准入、路径、等待与恢复。
- [远程部署](remote-execution.md)：Controller 与执行环境准备。
- [episode 执行器](episode-executor.md)：并发、完成台账与结果复用。
- [只读资产与执行边界](../agent-inputs/shared/execution.md#远程资产输出与额度)。
- [研究 skill](../skills/a3-mujoco-autoresearch/SKILL.md)：实验方法、诊断与经验复用。

## 路线与历史材料

[开发路线](roadmap.md)记录当前机制与研究方向。`plans/`、`reviews/` 和 [archive/](../archive/README.md) 中的旧阶段输入保留相应时期的设计与验收条件；当前启动流程以本页链接的教程和执行接口为准。第三方源码归属见 [third_party/README](../third_party/README.md)。
