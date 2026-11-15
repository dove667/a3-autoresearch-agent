# 源码职责

| 位置 | 负责的工作 |
| --- | --- |
| `harness/` | Agent 模型循环、研究工具、计划、上下文与知识读取 |
| `host/` | 本地进程、Gateway、认证转发、输入版本、暂停恢复和发布 |
| `execution/` | 远程 Controller、作业执行、GPU 调度、episode 批次与资产封存 |
| `cli/` | 用户与操作者的命令入口 |
| `path_safety.py` | 相对路径及符号链接检查 |
| `harness_payload.py` | 已审核的 Harness 源码载荷清单 |
| `token_count.py` | 离线 token 数量估算 |

`host/source_snapshots.py` 准备源码快照，`host/remote_client.py` 负责通过 SSH 调用远程 Controller。

`harness/history.py` 保存和检索原始执行历史，`harness/long_term_memory.py` 管理主动提炼的长期经验，`harness/operational_state.py` 记录工具交付、读取和作业进度。`harness/context/` 负责工作上下文与完整摘要，`skill_registry.py` 负责可复用方法。

实际研究数据位于项目根 `workspace/`，长期记忆位于根 `memory/`。源码目录中不保存运行工作区。完整边界与数据流见[系统架构](../../docs/reference/architecture.md)，数据位置见[目录职责](../../docs/reference/storage-layout.md)。
