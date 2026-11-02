# 项目文档

冷启动的目标是让通用 SWE Agent 自行形成面向 A3 的 auto research Harness，并验证 Policy 优化、Harness 反思、实验隔离与持久记忆。

- [环境配置](setup.md)：依赖、模型服务与 A3 执行环境。
- [使用教程](tutorial.md)：安装准备、全部启动参数、初始运行、中断恢复、版本迭代、轨迹与故障定位。
- [系统架构](architecture.md)：两阶段 RSI、运行单元、调用链、版本与身份、权限和资源管理。
- [目录职责](project-structure.md)：源码、候选、报告和运行产物的存放位置。
- 第一阶段[能力目标](../evals/phase-one/goal.md)与[验收标准](../evals/phase-one/acceptance.md)：默认启动输入，记录当前版本与内容。
- [第一阶段联网补充任务](plans/2026-11-02-phase-one-web-extension.md)：从保留 Harness 新建 E-only run；[目标](../evals/phase-one-web/goal.md)、[验收](../evals/phase-one-web/acceptance.md)与[执行协议](../configs/prompts/phase-one-web.md)独立版本化，历史 A–D 不重跑或重新认证。
- [第二阶段方案](plans/2026-10-31-phase-two.md)：完整网格下的 Policy 与 Harness 联合优化；独立目标、评估与完整 Prompt 已接入阶段选择；离线环境、预算配置与只读大文件交接入口已提供，真实训练和评测由 Agent 执行。
- [研究网页网关方案](plans/2026-11-02-research-web-gateway.md)：可选 Tavily 搜索、公开网页读取与第二阶段工具自实现；配置入口见环境配置。
- [Agent 输入版本与热加载](agent-inputs.md)：版本号维护、恢复加载规则和警告确认。
- [Host 执行接口](../runtime/local/README.md)：给生成后的 Harness 使用的固定部署能力。

从首次使用开始，先读使用教程；理解运行机制再读系统架构，查找文件归属时读目录职责。

开发状态见[路线与实现状态](roadmap.md)，早期设计与实施记录保存在 `plans/`。
