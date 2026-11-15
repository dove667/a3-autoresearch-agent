# Agent 输入管理

Host 为每次运行加载一套版本化部署输入，保留完整副本、语义版本与 SHA256。研究代码、计划和自写 skills 保存在根目录 `workspace/`，主动长期经验保存在 `memory/entries.jsonl`；恢复与 Harness 升级继续使用。

## 当前输入

项目的长期目标直接写在 `prompt/SOUL.md`：持续提高 A3 2×5、2×10 两类任务在全量评测中的准确率与通过率。Agent 围绕结果提出假设、安排实验、更新研究路线，由操作者暂停和恢复。

| 来源 | 输入名称 | 用途 |
| --- | --- | --- |
| `prompt/SOUL.md` | `SOUL.md` | 长期研究目标、决策方式与职责边界 |
| `prompt/execution.md` | `execution.md` | 工具选择、作业操作与异常处理 |
| `prompt/workspace.md` | `workspace-instructions.md` | 本地与远程路径、持久化和输入定位 |

Host 将三个 prompt 和研究方法 skill 保存为版本快照。SOUL 正文进入主 Agent 模型请求；执行和工作区说明通过 `A3_INPUT_ROOT` 指定的目录按需读取。输入清单位于 `host/inputs/versions.py`，`docs/` 面向操作者，不注入模型。

`prompt/` 平铺上述三个文件。研究原则集中在 SOUL；执行和工作区说明按需读取，工具定义直接提供具体参数。`input-manifest.json` 的 `execution_index` 提供执行说明的章节与字符范围，方便定位。

研究方法包统一由仓库 `skills/` 发布。模型、数据和评测设置随具体实验记录，完整全量评测用于衡量进展。启动与恢复命令见[教程](../guides/usage.md)。

早期阶段目标、验收、专项指令与 bootstrap 提示词原文保存在 [archive/agent-inputs/](../../archive/README.md)，当前运行不加载它们。

## 版本与发布记录

Markdown 在标题后写 `Version: 0.1.0`；YAML 使用顶层 `version: 0.1.0`。修改任何 Agent 可读部署内容都要递增该文件版本。同名同版本内容变化会被拒绝，防止历史标识对应多个正文。新增文件加入加载清单。

`inputs/input-manifest.json` 保存 revision、generation、输入源路径、版本和 SHA256。revision 定位一组确定内容；generation 在每次发布尝试时增加。读多个文件时，读取前后 manifest 都应为 active，且 revision 与 generation 一致。

发布期间 manifest 标记 updating，全部文件切换完成后标记 active。`input-revisions/` 保存完整历史内容，`input-events.jsonl` 保存切换事件。原始启动记录保持不变，恢复事件单独追加。

运行期间不会自动跟随仓库文件变化。操作者可以预览内容差异并确认具体摘要后更新 prompt 和 skills 部署输入；Agent 令牌不能调用更新接口。已发出的模型请求使用原输入，后续请求采用新版本。更新不改研究代码、对话或记忆，不替换进程中的工具实现。操作见[操作者监督](../guides/maintenance.md)。

## 发布 Harness 与恢复

Harness 源码由明确的 Git commit 固定。普通恢复要求完整模型设置与原发布一致，并继续同一身份、研究目录和上下文。维护者对暂停 run 执行显式发布事务时，可以更新源码、模型配置和部署输入，归档边界并保留研究状态。

首次启动选择已发布的 Harness 提交。后续恢复与原地升级沿用原 run、身份和工作区，保留对话、计划、记忆、skills 和作业状态。具体操作见[教程](../guides/usage.md)。

## 方法技能与研究记忆

Host 从根目录 `skills/` 发现经过审核的完整方法包，要求每包包含 `SKILL.md` 和 `assets/catalog-entry.json`。当前包覆盖 MuJoCo 研究流程、Policy 集成、作业恢复、帧缓存、训练进度和模型来源核对。公共输入与夜间发布包携带各包正文、参考资料、模板和 helper。

整包使用 `assets/catalog-entry.json` 的语义版本，每个文件单独记录 SHA256；增删或修改附件也需递增包版本。

Harness 启动时安装方法包，通过 `skills(action="list")` 检索，按需读取正文和附件。多关键词分别匹配并排序。恢复保留 Agent 已修改的技能副本，新部署参考包以独立版本加入。

Agent 自写技能保存在 `workspace/skills/`，主动长期经验和证据引用保存在 `memory/entries.jsonl`。技能工具维护目录、版本与正文快照；更新要求原内容哈希，冲突时重新读取。工具定义提供入口，SOUL 指导按需发现与使用。技能正文不全文加入每轮 Prompt。

维护者可以从 `workspace/skills/` 提取选定普通文件，核对来源版本与 SHA256后审阅。将通用方法回流仓库前检查私人信息、引用、索引与版本；发布输入不会自动合并或覆盖 Agent 的自写技能。

## 显式反馈与等待通知

人工反馈绑定当前主身份，以反馈 ID、内容摘要和独立 user message 记录。副本、投递事件与状态保存在 `.host/human-feedback/`。同内容去重，工具调用与结果保持配对；成功转发只表示已投递。Agent 记录采纳决定及其影响。

`GET /wait/signals` 返回本身份通知游标、反馈数量、输入版本和主身份状态。等待工具在程序层查询信号和作业状态；游标随 journal/checkpoint 恢复，通知不会启动额外 Agent。参数与执行行为见[执行接口](../../prompt/execution.md)。

独立 episode 执行器随远程 Controller 部署，输入更新不会替换已启动作业的程序。执行器契约见[episode 执行器](episode-executor.md)。
