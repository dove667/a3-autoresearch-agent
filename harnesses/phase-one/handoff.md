# 全新身份的任务交接

本入口加载阶段一暂时保留的原始 Harness。当前任务、验收和部署能力分别在只读的
`/run/input/goal.md`、`acceptance.md`、`execution.md`、`deployment.json`。
以本次文件中的全部验收项为准，包含当前新增的记忆能力。

这是新会话，没有旧身份、旧作业权限或进程内存。历史运行可作为外部提供的证据，
未提供的历史文件不可假定存在；需要恢复的先例由本次授权交接材料定位。
代码中的历史会话提示保留原样。缺少 ancestor-trajectory.jsonl 或 reflection.md 时，
记录其缺失并依据当前任务继续，不伪造既往执行。

候选版本、任务分解、auto research、记忆与反思的实现和选择由运行中的 Agent 决定。
人类提前准备的 Git 版本、协议检查和受控执行接口见 execution.md。
