# 项目文档

夜间由单个 Agent 自主研究 Policy、采集、训练、评估与分析结果。白天由人类和 Codex 依据原始执行轨迹维护 Harness，包括模型调用、工具、上下文、记忆机制、调度与恢复。Agent 负责报告过程问题及证据，并继续修改研究代码和管理研究知识。

- [环境配置](setup.md)：依赖、模型服务与 A3 执行环境。
- [使用教程](tutorial.md)：安装准备、全部启动参数、初始运行、中断恢复、版本迭代、轨迹与故障定位。
- [系统架构](architecture.md)：两阶段 RSI、运行单元、调用链、版本与身份、权限和资源管理。
- [目录职责](project-structure.md)：源码、候选、报告和运行产物的存放位置。
- 第一阶段[能力目标](../agent-inputs/phase-one/goal.md)与[验收标准](../agent-inputs/phase-one/acceptance.md)：默认启动输入，记录当前版本与内容。
- [第一阶段联网补充任务](plans/2026-11-02-phase-one-web-extension.md)：从保留 Harness 新建 E-only run；[目标](../agent-inputs/phase-one-web/goal.md)、[验收](../agent-inputs/phase-one-web/acceptance.md)与[执行协议](../agent-inputs/phase-one-web/instructions.md)独立版本化，历史 A–D 不重跑或重新认证。
- [第二阶段研究目标](../agent-inputs/phase-two/goal.md)：六格 Policy 研究和交付标准。
- [研究网页网关原方案](plans/2026-11-02-research-web-gateway.md)：历史设计与验证记录；当前采用 [Tavily 搜索／正文提取、专用本地代理与结果缓存](plans/2026-11-02-tavily-proxy-extract.md)，配置入口见环境配置。
- [Agent 输入版本与热加载](agent-inputs.md)：版本号维护、恢复加载规则和警告确认。
- [上下文恢复补丁与模型配置](context-reliability.md)：可选派生包、持久调用预算、状态保存及推理参数。
- [单研究 Agent 工作流](research-orchestration.md)：研究职责、独立作业并行与人工维护。
- [运行遥测](operational-telemetry.md)：脱敏请求、耗时、token 与操作计数。
- [视觉过程诊断](visual-diagnostics.md)：已有视频与轨迹优先、原生诊断保存范围、按问题选帧及只读视觉问诊边界。
- [Host 执行接口](../agent-inputs/shared/execution.md)：给生成后的 Harness 使用的固定部署能力。

从首次使用开始，先读使用教程；理解运行机制再读系统架构，查找文件归属时读目录职责。

开发状态见[路线与实现状态](roadmap.md)，早期设计与实施记录保存在 `plans/`。

- [远程执行端部署](remote-execution.md)：Controller 入口、镜像准备、资源调度与验证。

- [远程进程后端](process-backend.md)：数据盘路径、整机资源调度与 Docker 作业平滑退出。
