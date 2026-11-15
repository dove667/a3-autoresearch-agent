# Agent 提示词与技能管理

Host 为每次运行保存三个版本化 prompt 的完整副本、语义版本与 SHA256。研究文件使用共享 `workspace/`，技能统一使用根 `skills/`，长期经验由 `memory` 工具管理。

## 三个 prompt

长期目标直接写在 `prompt/SOUL.md`：持续提高 A3 2×5、2×10 两类任务在全量评测中的入盒准确率与通过率。Agent 持续研究，由操作者暂停和恢复。

| 来源 | 输入名称 | 用途 |
| --- | --- | --- |
| `prompt/SOUL.md` | `SOUL.md` | 长期目标、研究决策与职责边界 |
| `prompt/execution.md` | `execution.md` | 工具选择、作业操作与异常处理 |
| `prompt/workspace.md` | `workspace-instructions.md` | 路径、访问方式与持久化 |

`host/inputs/versions.py` 只加载这三个 prompt。SOUL 正文进入模型请求，另两份说明从 `A3_INPUT_ROOT` 按需读取。`input-manifest.json` 的 `execution_index` 提供执行说明的章节与字符范围。

历史阶段目标、验收和专项指令原文保存在 [archive/agent-inputs/](../../archive/README.md)。具体模型、数据和评测设置随实验保存，完整全量评测用于衡量研究进展。

## 版本与发布记录

Markdown 在标题后声明 `Version: 0.1.0`。修改 prompt 正文时递增版本；同名同版本内容变化会被拒绝。

`inputs/input-manifest.json` 保存 revision、generation、源路径、版本与 SHA256。连续读多个输入时，前后 manifest 都应为 active，且 revision 与 generation 一致。发布过程中标记 updating，全部文件切换完成后标记 active。

`input-revisions/` 保存不可变的历史输入，`input-events.jsonl` 保存切换事件，原始启动记录保留。编辑仓库文件不会自动更新活动输入；操作者预览内容并确认摘要后发布。Agent 令牌无输入更新权限。操作见[维护与发布](../guides/maintenance.md)。

## 发布 Harness 与恢复

Harness 源码由完整 Git commit 固定。普通恢复继续同一 run、身份、研究文件和会话状态，并核对原发布与模型配置。暂停期间的显式发布事务可以更新源码、模型配置和 prompt，归档旧边界后继续研究。具体步骤见[使用教程](../guides/usage.md)。

技能库独立于 prompt revision，由 `skills` 工具维护版本与修改历史。Harness 升级继续使用根技能库。

## 唯一技能库

根 `skills/` 是全项目唯一方法库。所有技能使用相同的访问权限，Agent 可通过 `skills` 创建、修改、归档正文和附件。普通文件工具不直接读取或编辑该目录。每包以 `SKILL.md` 和 `assets/catalog-entry.json` 声明身份、版本与正文位置；目录导航见[技能索引](../../skills/README.md)。

渐进读取：`list` 返回名称、描述和匹配摘要；`read` 返回选定正文；`list_resources` 列出附件；`read_resource` 分页读取所需文本附件。正文和附件均返回内容哈希，二进制附件只返回类型与摘要信息。

| 动作 | 更新条件 |
| --- | --- |
| `save` | 新建技能；更新已有正文时提供 `read` 返回的 `expected_sha256` |
| `archive` | 提供当前正文哈希；从默认发现中隐藏，历史仍可读，`save` 可恢复 |
| `write_resource` | 新附件目标必须不存在；覆盖已有附件时提供该附件当前的 `expected_sha256` |
| `delete_resource` | 提供该附件当前的 `expected_sha256` |

`read`、`read_resource` 按字符分页，单页最多 8000 字符；`list`、`list_resources` 每页最多 24 项。附件路径相对包根，控制文件和隐藏路径由工具保护。工具保存文本，不执行技能代码；研究计算仍通过远程作业。

任一包内容改动自动递增补丁版本，同步正文 frontmatter 与 catalog，并把完整旧包保存到 Host 管理的 `.runtime/skill-revisions/`。正文、附件和修改历史持续保留；哈希冲突时先重读再合并。

## 长期经验与原始历史

`memory` 检索和写入 `memory/entries.jsonl` 中的主动经验、适用条件与证据引用。`recall` 从当前会话的原始消息和工具历史检索观测。机器记录位于 `trajectories/<开始时间>/conversation/`，具体目录见[存储布局](storage-layout.md)。

## 显式反馈与等待通知

人工反馈绑定当前主身份，以 feedback ID、内容摘要和独立 user message 保存。副本与投递事件保存在 `.host/human-feedback/`，Agent 记录实际采纳与行动。

`GET /wait/signals` 返回通知游标、反馈数量、输入版本和主身份状态。等待在程序层观察作业状态与信号，游标随会话状态恢复；正常运行和排队不产生额外模型轮询。执行方式见[执行约定](../../prompt/execution.md)。
