# Agent 输入的版本与加载

Version: 0.4.0

本文件面向维护输入的部署方；运行监督与审批见 [操作者文档](operator-supervision.md)。目标、验收、Prompt 和执行说明由部署方维护。新启动、`--resume-from` 和 `--run-harness` 都采用启动时仓库里的当前文件，包括未提交的工作区内容。恢复保留原 run、身份、生成的 Harness 和模型对话历史；部署方 system Prompt 与所选阶段协议更新为当前版本。

## 文档职责与读者

| 文档 | 主要读者与职责 | 不承担的职责 |
| --- | --- | --- |
| `agent-inputs/<phase>/goal.md` | Agent：研究目标、范围、什么不算收益 | 操作者审批、接口操作手册 |
| `agent-inputs/<phase>/acceptance.md` | Agent / 复核者：完成条件、证据与终态交付 | 将人工建议当通过标签 |
| `agent-inputs/phase-two/protocol.md` | 第二阶段 Agent / 复核者：固定科学比较与归因规则 | 外界调度审批 |
| `agent-inputs/SOUL.md` | 主 Agent / 接任候选：跨阶段长期角色、主动工作和经验复用原则 | 新增阶段目标、修改科学计分或权限 |
| `agent-inputs/<phase>/instructions.md` | 主 Agent / 接任候选：行动、角色、记忆、收到反馈后的处理 | 审核外界批准、教授 operator CLI |
| `agent-inputs/shared/execution.md` → `execution.md` | Agent：真实可调用 API、状态、权限和生效技术事实 | 操作者 CLI 教程 |
| 本文件 / [教程](tutorial.md) | 维护者 / 用户：输入装载、版本、使用入口 | 新增科学验收门槛 |
| [operator-supervision.md](operator-supervision.md) | 用户 / 父助手 / 操作者：低频弱监督、每次干预的批准与执行核对 | 作为 Agent input 或替 Agent 做验收 |

源码统一位于 `agent-inputs/`，容器内文件名保持不变：SOUL + 阶段 Prompt + goal + acceptance（第二阶段加 protocol）直接进入主身份/接任候选模型请求；execution 等其余输入保持 `/run/input` 可读。不是仅给模型链接而拿走规则正文。普通 worker 仍由主 Agent 提供任务。`docs/` 下操作者文档不加入 `runtime/local/input_versions.py` 清单，也不注入 Agent 请求；SOUL 的装载链有离线回归覆盖；是否实际采用仍以对应运行的输入版本和模型调用记录为准。

目录结构见 [统一输入目录](../agent-inputs/README.md)。本次仅整理源路径：除 execution 的源路径说明修正并递增到 0.8.2 外，输入正文与语义版本不变。源路径进入 manifest 摘要，因此启动／恢复时产生新 revision；历史 run 的 manifest、input-revisions 与旧源路径记录不追改。`docs/plans/` 保留当时设计与路径，不作为当前加载映射；当前映射以本页和 `input_versions.py` 为准。

## 长期行为与 Agent 自有知识

`agent-inputs/SOUL.md` 定义跨阶段的研究者角色、主动行动、经验复用与诚实反馈原则；阶段 Prompt 保留具体流程，goal/acceptance/protocol 分别保持目标、完成证据和科学规则职责。agent-inputs/SOUL.md 由 Host 版本化，作为 /run/input/SOUL.md 提供，并进入主身份与接任候选的模型输入，不靠 Agent 偶然发现文件。普通测试 worker 仍由主 Agent 编写具体任务，不自动承担全部研究目标。

Agent 自己积累的 skills 是工作区可复用知识与代码，不是部署方版本化指令或新增权限。实际 E 保留包尚无统一技能发现、加载和保存机制；这些工具能力交给下一轮第一阶段 F，由 Agent 自主实现，设计见 [F 任务草案](plans/2026-11-02-phase-one-skills-f.md)。F 的版本化输入已接入 skills-extension profile，但本次不会代写技能工具或启动实验。

SOUL 与 Prompt 发布只提供行为指导，不能证明实际 Agent 已创建、加载或验证 skills。F 完成后还需用真实调用与恢复证据确认；保留清单不能自动替代技能发现和使用。本轮不会修改已完成 E 的保留包，也不自动启动第二阶段或热加载。

## 需要维护版本的内容

| 源文件 | Agent 可读位置 |
| --- | --- |
| `agent-inputs/SOUL.md` | `/run/input/SOUL.md`，长期行为原则 |
| `agent-inputs/<phase>/goal.md` | `/run/input/goal.md` |
| `agent-inputs/<phase>/acceptance.md` | `/run/input/acceptance.md` |
| `agent-inputs/<phase>/instructions.md` | `/run/input/phase-instructions.md`，所选阶段的完整主 Agent 协议 |
| `agent-inputs/phase-two/protocol.md` | `/run/input/protocol.md`，仅第二阶段的评估协议 |
| `agent-inputs/shared/bootstrap.yaml` | `/run/input/bootstrap.yaml`，初始 mini 的 Prompt 与启动配置 |
| `agent-inputs/shared/execution.md` | `/run/input/execution.md` |
| `agent-inputs/shared/readiness-task.md` | `/run/input/readiness-task.md`，部署验证任务 |
| `agent-inputs/shared/workspace.md` | `/run/input/workspace-instructions.md`，构建工作区的 `harness/AGENTS.md` |

`<phase>` 为 `phase-one` 或 `phase-two`。两个阶段的目标、验收和 Prompt 对称维护；每个输入清单只包含一个阶段，并共用 agent-inputs/SOUL.md 长期原则。阶段一不加载阶段二文件，阶段二不加载阶段一 Prompt，不额外叠加重复的 common Prompt。启动参数与工作区说明共用固定接口，不包含阶段验收目标。

例外是显式 `--phase phase-one --task-profile web-extension`：加载 `agent-inputs/phase-one-web/{goal,acceptance}.md` 和 `agent-inputs/phase-one-web/instructions.md`，映射到相同 `/run/input` 文件名，三者正文直接注入主 Agent／接任候选，execution 保持可读。它不叠加普通第一阶段 A–D Prompt 或第二阶段 protocol；普通冷启动仍使用 `standard` profile 的原 A–D 输入。E-only 验收只有 `## E.` 能力项，不把继承元数据解析为新的 A–D pass。

Markdown 在标题之后写 `Version: 0.1.0`；YAML 顶层写 `version: 0.1.0`。这些版本独立维护。任何内容修改都需要递增：兼容性文字修正递增 patch，例如 `0.1.0 → 0.1.1`；能力、协议或验收变化通常递增 minor；不兼容重构递增 major。版本号与 SHA256 同时记录。同一 run 见过的同名同版本文件若内容变化，加载会被拒绝，须先修正版本号。

新增直接发给 Agent 的文件时，添加明确版本，并将其加入 `runtime/local/input_versions.py` 的输入清单。专项 Harness 自己生成的任务、记忆和提示词由 Agent 管理，本规则约束部署方发布的输入。

## 启动与恢复记录

新运行默认 `phase-one`，使用 `--phase phase-two --run-harness <明确来源>` 选择第二阶段。`--resume-from` 自动读取原 run 的阶段，显式指定冲突阶段会拒绝；跨阶段使用新 run。旧记录没有阶段字段时归属阶段一。热加载只更新当前阶段的文件。

联网补充只接受第一阶段明确保留结果，以 `--phase phase-one --task-profile web-extension --run-harness <第一阶段run目录>` 新建运行。`startup.json` 与 input manifest 保存 `task_profile`，缺省为 `standard`；恢复沿用原 profile，冲突选择拒绝，热加载也只更新该 profile。不要 resume 已 self-stop 的旧 run 来补 E。新副本初始化新上下文与 checkpoint，源保留快照不改；同 run resume 保留本轮上下文。`task_context.inherited_acceptance` 记录 A–D 的 `historical-not-recertified`、source_run 与 source_version，仅为来源元数据，不能证明新验收通过或修复原归档缺口。

web-extension 的新副本将已知旧任务状态归档至 `inherited-task-state/`，记录原文件内容摘要与来源；活动 `task.txt` 改为 E 目标，checkpoint 初始化新上下文，append-only trajectory 从空轨迹开始，旧 progress 完成状态只作继承记录，`run-outcome.json` 改为 continue 并要求新 E 证据，防止旧任务／终态或旧大轨迹污染新任务。`task_context.retired_terminal` 仍留存原终态摘要与来源。源 retained 和只读历史不变，同 run resume 不执行这种新任务初始化。继承的长期记忆不是待清空的旧任务状态。

具体归档只处理已存在的 task.txt、trajectory.jsonl、progress.md、run-outcome.json，不凭空创建缺失文件；`task_context.initialized_task_files` 逐文件记录 source_sha256、archive_path、source_run、source_version。旧 progress 原文保留，活动 progress 标记 E pending／历史未重新认证。归档路径或祖先／后代冲突时失败关闭，不覆盖材料。

可选 `--initial-feedback-file` 只用于已批准的新建第一阶段 web-extension 或 skills-extension retained run 输入，不能与 resume 或旧 `--feedback-file` 混用。读取非空 UTF-8、最多 64 KiB 的文件，在 fresh checkpoint 的目标消息后加入第二个 user message，并在新副本写 `initial-feedback.md`、在 task_context 留存摘要／字节数／来源。省略时仍只有目标消息，不自动附加固定或私有反馈。它不是动态 Host feedback 登记，不改既有运行反馈、热加载及其审批规则。

`startup.json` 和 `inputs/input-manifest.json` 保存 `phase`。`inputs/input-manifest.json` 列出整套输入的快照编号 revision、发布尝试序号 generation，以及每个文件的源路径、语义版本和 SHA256。revision 定位一组确定的文件；generation 在每次发布尝试时增加，更新失败回到旧快照也会增加，让读取程序知道期间发生过更新。两者均由 Host 自动维护，开发者维护的是各文件的语义版本。

发布开始时所有挂载先标记 status=updating（更新中），完成后才标记 active（可读取）。每次启动或更新的完整内容保存在 `input-revisions/revision-*/`；`input-events.jsonl` 保存切换记录。原始 `startup.json` 保持不变，恢复事件附带当前清单写入 `resume-events.jsonl`。旧运行首次升级时，原输入另存历史副本。

普通挂载与主会话的私有输入挂载同步发布。主 Agent 与接任候选每次模型请求都获得当前阶段 Prompt、目标和验收正文；第二阶段还提供 protocol.md；普通测试 worker 的任务与 Prompt 继续由主 Agent 提供。用量日志记录请求采用的 input_revision、各文件版本与摘要。初始 mini 刷新部署方 system 消息，保留已有用户消息、模型回复和工具结果。

全部 worker 停止后，Host 先关闭更新入口，再按最终生效的验收版本作结构检查，并把该版本清单写入 outcome。历史结果保留各自产生时的版本；变更验收后仅按影响范围判断相关已有结论是否适用，不将其扩为全历史重审。各代交权前核查自己的实验闭环，后继继承已确认结论及证据引用，不承担前任过去的验收责任；最终汇总仍须如实保留未完成与未知。

## 第一阶段 F 的独立输入

`--phase phase-one --task-profile skills-extension --run-harness <E run>` 选择 `agent-inputs/phase-one-skills/{goal,acceptance,instructions}.md`，仍映射到标准 /run/input 文件名，并共享 SOUL 和 execution。只验收 `## F.`，不叠加普通 A–D、E 或第二阶段任务。来源须是完成 E 且有 ready 保留包的第一阶段 web-extension run；不得把仓库模板、Agent 自述或历史 metadata 当作成功来源。

新 F run 沿用源码与持久记忆，初始化新的 F 目标上下文，旧 task、trajectory、progress、run-outcome（存在时）及 initial-feedback 归档到 inherited-task-state/skills-extension/，保留原有归档且不修改源 retained。旧初始反馈不再占用活动文件位置；只有显式提供当次通知才生成新的 initial-feedback.md。A–E 仅作为 historical-not-recertified 来源继承，不能充当本轮新增 pass。可提供当次已批准的 initial feedback；旧 E 的 initial-feedback.md 不自动成为 F 指令。恢复沿原 F profile 与完整上下文，不能通过 resume 把完成 E 切换为 F。

F 输入接入不是技能工具实现；实际模型发现、读取、执行、复用、更新及跨恢复能力须由后续 F 运行提供证据。当前准备工作不授权启动、热加载、训练或第二阶段。

## 运行期间热加载

编辑文件不会自动发布。每次热加载或中断前须用户明确批准；操作步骤唯一维护于 [操作者文档](operator-supervision.md#热加载版本化输入)。Agent 令牌不能调用控制入口。

已发出的请求按原版本返回，下次请求使用新版本。多文件读取前后 manifest.status 必须为 active，revision 与 generation 相同；updating 时等待，标识变化后重读。热加载只更新文件与 Prompt，不重建对象、不换模型或镜像。更新不启动额外 Agent、不替 Agent 选择实验、不生成验收结论。

## 显式人类反馈

恢复时与运行中均可显式登记反馈，完整操作和批准范围见 [操作者文档](operator-supervision.md#恢复时加入人类反馈)。反馈不是目标、验收或计分文件的替换；固定 0.1.0 封装协议生成独立 user message，以反馈 ID 和 SHA256 追踪。源文件可覆盖，无需用户手动改版本；封装协议变化需递增实现中的 PROTOCOL_VERSION 并同步文档。

副本、登记/定位/成功转发事件与状态保存在 `.host/human-feedback/`，不自动监视源文件。同一身份最近同内容反馈去重；反馈只绑定登记时主身份，不广播到 worker 或后继。有效历史前缀摘要定位注入位置，工具调用与结果保持配对，重建上下文时记录重新定位。模型用量 human_feedback_ids 标识该请求携带的反馈；forwarded 不表示 Agent 已采纳。没有反馈时原请求路径不变。Agent 的范围核查、决定及影响留档见各阶段 Prompt，外界批准流程只由操作者承担。
