# Agent 输入管理

Host 为每次运行加载一套版本化部署输入，保留完整副本、语义版本与 SHA256。研究代码、计划、记忆和自写 skills 位于研究工作区，随同一 run 的恢复与 Harness 升级继续使用。

## 当前输入

每个新研究任务必须通过 `--agent-inputs-dir <目录>` 明确提供版本化的 `goal.md`、`acceptance.md` 和 `protocol.md`。目标、完成证据与科学协议由任务定义，启动器没有阶段或专项 profile 选择。

| 来源 | 输入名称 | 用途 |
| --- | --- | --- |
| 任务输入目录 | `goal.md` | 当前研究目标 |
| 任务输入目录 | `acceptance.md` | 完成证据与验收项 |
| 任务输入目录 | `protocol.md` | 当前科学协议与比较条件 |
| `agent-inputs/SOUL.md` | `SOUL.md` | 研究 Agent 的通用角色与行为原则 |
| `agent-inputs/shared/execution.md` | `execution.md` | 工具和 Host 接口 |
| `agent-inputs/shared/workspace.md` | `workspace-instructions.md` | 原生研究工作区指引 |

Host 将当前公共输入和研究方法 skill 加入任务快照。SOUL、goal、acceptance、protocol 正文进入主 Agent 模型请求；执行接口等文件通过 `A3_INPUT_ROOT` 指定的目录按需读取。输入清单位于 `host/inputs/versions.py`，`docs/` 面向操作者，不注入模型。

任务目录可携带额外方法包和当次明确提供的 `initial-feedback.md`。这些文件按显式清单保存，不从历史阶段补齐。启动与恢复命令见[教程](tutorial.md)。

早期阶段目标、验收、专项指令与 bootstrap 提示词原文保存在 [archive/agent-inputs/](../archive/README.md)，当前运行不加载它们。

## 版本与发布记录

Markdown 在标题后写 `Version: 0.1.0`；YAML 使用顶层 `version: 0.1.0`。修改任何 Agent 可读部署内容都要递增该文件版本。同名同版本内容变化会被拒绝，防止历史标识对应多个正文。新增文件加入加载清单。

`inputs/input-manifest.json` 保存 revision、generation、输入源路径、版本和 SHA256。revision 定位一组确定内容；generation 在每次发布尝试时增加。读多个文件时，读取前后 manifest 都应为 active，且 revision 与 generation 一致。

发布期间 manifest 标记 updating，全部文件切换完成后标记 active。`input-revisions/` 保存完整历史内容，`input-events.jsonl` 保存切换事件。原始启动记录保持不变，恢复事件单独追加。

运行期间不会自动跟随仓库文件变化。操作者可以预览内容差异并确认具体摘要后更新当前任务输入；Agent 令牌不能调用更新接口。已发出的模型请求使用原输入，后续请求采用新版本。更新不改研究代码、对话或记忆，不替换进程中的工具实现。操作见[操作者监督](operator-supervision.md)。

## 发布 Harness 与恢复

Harness 源码由明确的 Git commit 固定。普通恢复要求完整模型设置与原发布一致，并继续同一身份、研究目录和上下文。维护者对暂停 run 执行显式发布事务时，可以更新源码、模型配置和部署输入，归档边界并保留研究状态。

新任务从选定 retained 结果加载发布版本及声明保留的知识，初始化新的目标、对话、计划和作业登记。同一 run 的恢复与原地升级继续使用既有研究状态。具体操作见[教程](tutorial.md)。

## 方法技能与研究记忆

当前公共输入和夜间发布包携带 `skills/a3-mujoco-autoresearch/` 的正文、参考资料、JSON 模板及 Python helper。整包使用 `assets/catalog-entry.json` 的语义版本，每个文件单独记录 SHA256；增删或修改附件也需递增包版本。

Harness 启动时安装方法包，通过 `skills(action="list")` 检索，按需读取正文和附件。多关键词分别匹配并排序。恢复保留 Agent 已修改的技能副本，新部署参考包以独立版本加入。任务输入还可以交付额外的明确选中方法包。

Agent 自写技能、具体经历和证据来源保存在研究工作区。技能工具维护目录、版本与正文快照；更新要求原内容哈希，冲突时重新读取。工具定义提供入口，SOUL 指导按需发现与使用。技能正文不全文加入每轮 Prompt。

维护者可以从 `workspace/<run-id>/<session-id>/skills/` 提取选定普通文件，核对来源版本与 SHA256后审阅。将通用方法回流仓库前检查私人信息、引用、索引与版本；发布输入不会自动合并或覆盖 Agent 的自写技能。

## 显式反馈与等待通知

人工反馈绑定当前主身份，以反馈 ID、内容摘要和独立 user message 记录。副本、投递事件与状态保存在 `.host/human-feedback/`。同内容去重，工具调用与结果保持配对；成功转发只表示已投递。Agent 记录采纳决定及其影响。

`GET /wait/signals` 返回本身份通知游标、反馈数量、输入版本和主身份状态。等待工具在程序层查询信号和作业状态；游标随 journal/checkpoint 恢复，通知不会启动额外 Agent。参数与执行行为见[执行接口](../agent-inputs/shared/execution.md)。

独立 episode 执行器随远程 Controller 部署，输入更新不会替换已启动作业的程序。执行器契约见[episode 执行器](episode-executor.md)。
