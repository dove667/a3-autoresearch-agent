# Agent 工作区说明

Version: 0.2.0

当前使用本地原生进程和持久研究工作区。模型和作业经 Host Gateway 转发，认证与资源配置由 Host 管理。
研究知识由 Agent 使用现有记忆与技能工具管理。
每轮运行状态中的 runtime_paths 给出工作区、输入与参考源码的实际目录。按 inputs 路径读取 goal.md、acceptance.md、protocol.md、execution.md 与 input-manifest.json 的当前版本。
研究 Agent 负责 Policy、数据采集、训练、评估、研究脚本和结果判断。Harness 的模型调用、工具注册、上下文、记忆机制、调度与恢复由人类和 Codex 在运行暂停期间维护。遇到这类问题时保留轨迹位置、实际影响和复现条件，交给维护者。夜间只使用部署方提供的 Harness，不修改运行框架、启动 Harness 候选或切换主身份。
互相独立且输入已就绪的远程作业，先在权限与资源额度内并行提交，再统一查询与收集结果；有数据依赖的作业按依赖推进。采集和评估中的独立 episode 统一使用作业提供的 `A3_EPISODE_EXECUTOR`（远程 Docker 为 `/run/a3/episode_executor.py`），由 Agent 固定科学参数、seed、协议与输出，复用已完成结果。
普通脚本、参数、依赖适配和结果解析问题由当前 Agent 在当前任务中定位、修复并验证到底，保留失败与修复证据；需要 Host 凭据、权限、服务或资源配置变更时，保存具体错误、所缺条件和所需操作，明确报告操作者。正常作业等待按状态继续推进。
Host 提供会话状态、暂停恢复及受控隔离验收接口。
