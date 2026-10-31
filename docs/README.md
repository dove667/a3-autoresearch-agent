# 项目文档

冷启动的目标是让通用 SWE Agent 自行形成面向 A3 的 auto research Harness，并验证 Policy 优化、Harness 反思、实验隔离与持久记忆。

- [环境配置](setup.md)：依赖、模型服务与 A3 执行环境。
- [使用教程](tutorial.md)：安装准备、全部启动参数、初始运行、中断恢复、版本迭代、轨迹与故障定位。
- [系统架构](architecture.md)：两阶段 RSI、运行单元、调用链、版本与身份、权限和资源管理。
- [目录职责](project-structure.md)：源码、候选、报告和运行产物的存放位置。
- [能力目标](../evals/goal.md)与[验收标准](../evals/acceptance.md)：每次运行会冻结自己的副本。
- [Host 执行接口](../runtime/local/README.md)：给生成后的 Harness 使用的固定部署能力。

从首次使用开始，先读使用教程；理解运行机制再读系统架构，查找文件归属时读目录职责。

开发状态见[路线与实现状态](roadmap.md)，早期设计与实施记录保存在 `plans/`。
