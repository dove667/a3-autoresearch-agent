# 当前 Agent 通用输入

这里只维护当前 auto research Agent 的通用提示词与接口说明：

- `SOUL.md`：角色、主动研究、经验复用与诚实反馈原则。
- `shared/execution.md`：工具、Host 接口、状态与权限。
- `shared/workspace.md`：原生研究工作区指引。

新研究任务通过 `--agent-inputs-dir` 明确提供版本化的 `goal.md`、`acceptance.md` 与 `protocol.md`。Host 将任务输入、上述通用输入和项目方法 skill 保存为完整版本快照。

历史阶段目标、验收、专项指令和 bootstrap 提示词原文位于 [archive/agent-inputs/](../archive/README.md)，当前启动器不加载。输入版本与使用规则见[输入管理](../docs/agent-inputs.md)。

本 README 供维护者导航，不加入模型输入。
