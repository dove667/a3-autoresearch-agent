# Agent 工作区说明

Version: 0.1.0

当前为独立 Docker 工作区。模型和作业经 Host gateway 转发，不得获取 Host 凭据或改变资源分配。
专项 auto research、Harness、Policy 与记忆由 Agent 自主实现。
读取 /run/input/goal.md、acceptance.md、execution.md 和 input-manifest.json 中的当前版本。
Host 提供候选执行、主身份交接、结束自身和受控双身份验收接口。
