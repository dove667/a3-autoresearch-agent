# 研究工作区

Version: 0.3.0

本地原生进程使用持久工作区。运行状态 `runtime_paths` 给出以下真实路径：

| 路径 | 用途 |
| --- | --- |
| `workspace` | 当前身份可写的研究目录；文件工具的相对路径从这里解析 |
| `inputs` | 只读提示词、方法包和 `input-manifest.json` |
| `reference` | 只读 A3 参考源码、README、接口文档与 examples |

`SOUL.md` 定义长期目标与研究原则，`execution.md` 说明工具和执行约定，均在 `inputs` 保存版本快照。本文件在输入快照中名为 `workspace-instructions.md`。

研究脚本放在 `research/`，以便通过 `workspace_files` 附加到远程作业。本地文件操作服务于准备和分析；仿真、训练与评估通过远程作业执行，服务器路径由作业环境提供。

工作区中的 `checkpoint.json`、工具 journal、计划、`skills/` 和 `context-history/` 保存恢复状态、研究代码与方法。`context-history/` 包含完整历史快照、长工具输出及摘要恢复记录，供 `recall` 按来源检索；保留工具返回的引用。主动提炼的长期经验独立存放在项目根 `memory/entries.jsonl`，由 `memory` 工具管理；运行状态 `runtime_paths.memory` 和环境变量 `A3_MEMORY_ROOT` 给出实际记忆目录。

项目使用唯一的根目录 `workspace/` 与 `memory/`。暂停、恢复或升级 Harness 时共用原研究状态和长期记忆，恢复后按已保存计划核对真实作业与产物。自写技能保存在工作区 `skills/`，进入公共 `skills/` 前由维护者审核。

输入快照以 manifest 的 revision、generation、status、逐文件版本和 SHA256 标识。跨文件读取时核对前后均为 active 且 revision/generation 一致；updating 时等待，版本变化后重读相关内容。同一已验证版本的已读内容按需复用。
