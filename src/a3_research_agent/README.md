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

`host/reference_sources.py` 准备按内容复用的只读参考源码缓存，`host/remote_client.py` 负责通过 SSH 调用远程 Controller。

`harness/history.py` 保存和检索模型可见历史，并沿引用检索完整工具输出。`harness/memory/` 管理主动提炼的长期经验，`harness/operational_state.py` 记录读取与作业观察。`harness/context/` 中 manager/compaction 负责预算与压缩事务，summary 校验研究交接，tool_output 提取有界指标与回执，facts 每轮刷新计划决策与原始观察，source_search 校验并流式检索原文。`skill_registry.py` 负责可复用方法。

实际研究数据位于项目根 `workspace/`，长期记忆位于根 `memory/`。源码目录中不保存运行工作区。完整边界与数据流见[系统架构](../../docs/reference/architecture.md)，数据位置见[目录职责](../../docs/reference/storage-layout.md)。

`harness/tool_catalog.py` 声明模型可见的高层工具；`file_tools.py`、`job_tools.py`、`host_tools.py` 和 `control_tools.py` 按领域处理，`tool_results.py` 统一成功、错误和提交未知回执。模型不获得通用 HTTP 工具。
