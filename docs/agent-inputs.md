# Agent 输入的版本与加载

Version: 0.6.4

本文件面向维护输入的部署方；运行监督与审批见 [操作者文档](operator-supervision.md)。目标、验收、Prompt 和执行说明由部署方维护。使用内置 profile 时，新启动、`--resume-from` 和 `--run-harness` 采用启动时仓库里的当前文件，包括未提交的工作区内容。显式自定义输入目录则使用下述独立冻结快照，不回退到仓库模板。恢复保留原 run、身份、生成的 Harness 和模型对话历史；部署方 system Prompt 与所选阶段协议更新为当前版本。

## 通用新任务：交接包 + 完整输入目录

已有 `--run-harness` 可以把任意有效的 ready 保留包用于新的任务，不需要为每个任务增加 E/F profile 或专用启动入口：

```bash
python -m a3_research_agent.host.bootstrap \
  --run-harness artifacts/rsi/<来源-run-id> \
  --agent-inputs-dir /absolute/path/to/my-task-inputs
```

`--run-harness` 的路径选择**来源 run ID 的交接包**（或现有支持的显式包目录），不改名、不写回来源。新 run ID 仍由 Host 自动生成。输入目录选择**本轮全部 Agent 输入**，没有指定的必需文件会报错，绝不悄悄补入旧 E/F 模板。原有 ready/快照完整性校验不变；源码路径上的模板版本或历史通过记录不能代替本轮验收。

输入目录使用容器内规范文件名，完整包含：

- `SOUL.md`
- `goal.md`
- `acceptance.md`
- `phase-instructions.md`（仅第一阶段必需）
- `bootstrap.yaml`
- `execution.md`
- `readiness-task.md`
- `workspace-instructions.md`

可选 `protocol.md`；若指定 `--phase phase-two` 则它必需，而 `phase-instructions.md` 不必提供，也不生成空壳。为兼容显式旧输入，第二阶段自定义目录若包含 `phase-instructions.md`，它仍需 Version 并作为附加阶段层注入、冻结与恢复；不能把该保留行为误认为新 preset 仍有该文件。可选 `initial-feedback.md` 放入当次获批的人工干预说明。其他 UTF-8 文本文件或子目录也按原相对路径完整快照并挂载到 `/run/input`。必需文件沿用语义版本约定；附加资料和初始反馈可以没有 Version 行，仍记录内容哈希。禁止符号链接、非普通文件和 Host 保留路径（如 `deployment.json`、`input-manifest.json`、`history/`），避免输入冒充运行元数据。不会读取整个项目目录或自动寻找密钥；请只传专门整理的 Agent 输入目录。

SOUL、goal、acceptance、存在的 phase-instructions 以及 protocol 正文直接进入主 Agent/接任候选的模型请求。其他文件**挂载可读但不自动全文注入**，可在目标/指令中说明用途。`initial-feedback.md` 经既有反馈内容校验后作为新 checkpoint 的第二条 user 消息注入，附 SHA256，task_context 记录来源文件与摘要；它不是运行中热加载。不要把这项通知当成验收通过标签。普通 worker 仍由主 Agent 分配任务。

自定义验收用唯一命名标题，例如 `## skill_reuse. 真实工具闭环`，Host 据此检查 `acceptance.skill_reuse`，不限定 A–F。也可继续使用 `## A.` 等旧格式。至少需要一个验收项；重复 ID 或无可识别项会在启动前失败。

新任务继承包中的 harness、代码、skills、长期记忆，但重新初始化 checkpoint、task.txt、trajectory.jsonl、progress.md 和 run-outcome.json（continue），不会因来源 F 已 complete 而立即结束。旧任务文件和旧初始反馈保存在副本的唯一 `inherited-task-state/<来源摘要>/` 下，并记录原摘要及来源；旧反馈不再次成为活动指令。继承的验收只记为历史未重新认证。保留包需要支持现有 `checkpoint.json` 消息列表格式，不猜测未知 Harness 的上下文布局。

启动前验证并读取整套输入；实际采用的字节、来源、版本和哈希写入 run 的 `input-revisions` 与 manifest，后续不依赖原目录是否还存在。自定义 run 的 `--resume-from <新run目录> --feedback-file <获批说明>` 保留这轮完整现场，继续使用已记录的冻结输入快照。不会因为仓库模板或原输入目录改了而换任务；若操作者已获准替换自定义输入，可通过现有热加载命令显式选择完整的新目录，预览并确认其摘要；仅接受同一阶段、同一 profile 和相同文件名集合，版本规则保持有效。发布生成新快照，旧快照不改；恢复继续使用最后正式发布的冻结版本。跨阶段或新任务仍另建 run。动态 human feedback 的既有接口及审批要求不变。

launch 的 `--agent-inputs-dir` 只用于新 `--run-harness`，不能与 resume、部署检查、E/F 专项 profile 或 `--initial-feedback-file` 混用。自定义初始反馈统一放进输入目录。旧内置 profile 保持兼容，用于已有流程/历史恢复；通用新任务无需选择这些专项 profile。

## G：使用通用入口装配上下文管理任务

G 的源文件位于 `agent-inputs/phase-one-context/`，不注册新的 task profile。部署方在启动前核对选定来源确为 F ready 成果及当次初始反馈；使用已有 `--run-harness` 与完整自定义输入目录，新 run 不覆盖源包。本节只说明装配，不会启动任务。

在仓库根目录准备独立输入目录（不要把整仓库作为输入）：

```bash
inputs=$(mktemp -d)
cp agent-inputs/SOUL.md "$inputs/SOUL.md"
cp agent-inputs/phase-one-context/goal.md "$inputs/goal.md"
cp agent-inputs/phase-one-context/acceptance.md "$inputs/acceptance.md"
cp agent-inputs/phase-one-context/instructions.md "$inputs/phase-instructions.md"
cp agent-inputs/phase-one-context/initial-feedback.md "$inputs/initial-feedback.md"
cp agent-inputs/shared/bootstrap.yaml "$inputs/bootstrap.yaml"
cp agent-inputs/shared/execution.md "$inputs/execution.md"
cp agent-inputs/shared/readiness-task.md "$inputs/readiness-task.md"
cp agent-inputs/shared/workspace.md "$inputs/workspace-instructions.md"
```

确认当次来源、模型和预算后，将该目录交给既有启动命令：

```bash
python -m a3_research_agent.host.bootstrap \
  --run-harness artifacts/rsi/<选定的-F-run-id> \
  --agent-inputs-dir "$inputs"
```

本轮只有 `acceptance.G`；不传 E/F 专项 profile，不加载 phase-two protocol。初始反馈通过目录里的文件注入，不另传 `--initial-feedback-file`。若需要随新任务提供研究 skill，可把已检查的 `skills/research-orchestrator/`、`skills/a3-mujoco-autoresearch/` 与 `skills/rsi-harness-evolution/` 纯文本文件树复制到该输入目录同名子路径。复制时排除运行生成的缓存，例如 `mkdir -p "$inputs/skills"` 后运行 `rsync -a --exclude='__pycache__/' --exclude='*.pyc' --exclude='*.tmp' skills/research-orchestrator skills/a3-mujoco-autoresearch skills/rsi-harness-evolution "$inputs/skills/"`；仍须核对输入只含 UTF-8 普通文件。三者只读挂载，不自动注入正文或自动登记为 Agent 的活动 skill。正文读取、未来工作区复制及目录合并见 skill 自带集成说明，这不是 G 的新增验收项目。

### 研究协作的职责与装载

SOUL 只保留主 Agent 协调、方向、派发和判断，Policy/Harness worker 负责具体实现与执行的职责摘要。[research-orchestrator](../skills/research-orchestrator/SKILL.md) 维护工作包、证据回传、显式资产授权和安全交权细节；RSI 的 allocation 维护工作价值权衡，A3 skill 维护可选研究方法。简单协调由主 Agent 直接完成，不要求为每次读状态或记决定再创建 worker。

新 skill 的 [catalog fragment](../skills/research-orchestrator/assets/catalog-entry.json) 沿用 F 的 reference-only 形式：`skill_discover` 查元数据，`skill_read` 返回元数据，正文按 `local_source` 用普通 read 读取，不增加通用 `skill_execute`。需要持久采用时，在获准的新工作区合并 `skills/index.json` 的 skills 数组并检查保留声明。复制目录、更新 SOUL 或合并 catalog 本身不会安装自动派发、限制主工具或迁移旧 worker 的监督权；实际执行保障须检查已加载的 Harness 代码和验证证据。源码准备不表示当前 run 已采用，也不授权热加载。

启动后应核实实际 input manifest、`acceptance_checks=["G"]`、新任务状态及初始反馈消息，不能只看仓库文件。自定义 run 的恢复使用其冻结输入；每次人工恢复的说明继续走已有 feedback 流程。G 通过保存退出，不自动开始第二阶段。准备输入或发布源码本身不证明 Agent 已实现上下文管理。

## 文档职责与读者

| 文档 | 主要读者与职责 | 不承担的职责 |
| --- | --- | --- |
| `agent-inputs/<phase>/goal.md` | Agent：研究目标、范围、什么不算收益 | 操作者审批、接口操作手册 |
| `agent-inputs/<phase>/acceptance.md` | Agent / 复核者：完成条件、证据与终态交付 | 将人工建议当通过标签 |
| `agent-inputs/shared/protocol.md` | 第二阶段 Agent / 复核者：固定科学比较与归因规则 | 外界调度审批 |
| `agent-inputs/SOUL.md` | 主 Agent / 接任候选：跨阶段长期角色、主动工作和经验复用原则 | 新增阶段目标、修改科学计分或权限 |
| `agent-inputs/phase-one*/instructions.md` | 第一阶段主 Agent / 接任候选：本轮专项能力任务流程 | 审核外界批准、教授 operator CLI |
| `agent-inputs/shared/execution.md` → `execution.md` | Agent：真实可调用 API、状态、权限和生效技术事实 | 操作者 CLI 教程 |
| 本文件 / [教程](tutorial.md) | 维护者 / 用户：输入装载、版本、使用入口 | 新增科学验收门槛 |
| [operator-supervision.md](operator-supervision.md) | 用户 / 父助手 / 操作者：低频弱监督、每次干预的批准与执行核对 | 作为 Agent input 或替 Agent 做验收 |

源码统一位于 `agent-inputs/`，容器内文件名保持不变：SOUL + goal + acceptance，第一阶段另加任务 Prompt，第二阶段另加 protocol，直接进入主身份/接任候选模型请求；execution 等其余输入保持 `/run/input` 可读。不是仅给模型链接而拿走规则正文。普通 worker 仍由主 Agent 提供任务。`docs/` 下操作者文档不加入 `src/a3_research_agent/host/inputs/versions.py` 清单，也不注入 Agent 请求；SOUL 的装载链有离线回归覆盖；是否实际采用仍以对应运行的输入版本和模型调用记录为准。

目录结构见 [统一输入目录](../agent-inputs/README.md)。早期目录统一迁移仅改路径并将 execution 递增到 0.8.2；这是历史迁移说明。当前第二阶段已移除单独 instructions，protocol 迁入 shared 但只在该阶段加载，各输入按实际改动独立递增版本。源路径进入 manifest 摘要，因此启动／恢复时产生新 revision；历史 run 的 manifest、input-revisions 与旧源路径记录不追改。`docs/plans/` 保留当时设计与路径，不作为当前加载映射；当前映射以本页和 `input_versions.py` 为准。

## 长期行为与 Agent 自有知识

`agent-inputs/SOUL.md` 定义跨阶段的研究者角色、主动行动、经验复用与诚实反馈原则；第一阶段任务 Prompt 保留专项流程；第二阶段不再有独立 Prompt 文件，goal/acceptance/protocol 分别保持目标、完成证据和科学规则职责。agent-inputs/SOUL.md 由 Host 版本化，作为 /run/input/SOUL.md 提供，并进入主身份与接任候选的模型输入，不靠 Agent 偶然发现文件。普通测试 worker 仍由主 Agent 编写具体任务，不自动承担全部研究目标。

Agent 可主动把可复用方法写入自己的工作区 skill；具体经历、证据与待办进入长期记忆。SOUL 提供任务切换、重复失败与派发前的发现提示；实际读取、沉淀和复用由 Agent 执行。部署没有自动按任务匹配和注入 skill 正文的触发器。

当前派生 Harness 内置 `skills` 工具：list 按关键词搜索摘要并提示未登记正文，read 返回有界正文与哈希，save 自动维护目录、版本和不可变正文，archive 隐藏默认发现但保留内容。更新与归档要求传入读取到的原内容哈希，冲突须重新读取合并。目录采用文件锁与原子替换，当前正文保持稳定路径，旧正文快照进入 `skills/<id>/revisions/`。save 可设置 worker_roles 为 policy、harness 或两者，派发对应角色时自动携带该 skill 的当前正文和同目录引用文件，跳过历史 revisions；新技能省略角色时不自动分发，现有 Worker 副本不自动更新。普通引用文件仍由 Agent 在工作区维护。旧 skill_discover/skill_read 仍可搜索同一 JSON 目录；新工具没有接管旧 synthetic checker 的执行授权。完整参数见[执行说明](../agent-inputs/shared/execution.md#工作区技能的读取与沉淀)。

工具 schema 只提供使用入口，不自动注入全部技能正文。SOUL 指导 Agent 在任务切换、重复失败和委派前按需使用。没有新工具的历史 Harness 仍可使用普通 read/write；另建 LOCAL-INDEX.md 不会自动进入 JSON 目录搜索。新增 Python 模块和 tool schema 需恢复进程加载，单独热加载 Prompt 不会安装工具。

### 运行中的技能导出与回流

Docker `/workspace` 挂载到对应候选的 Host 工作区。Agent 写入的正文、索引与记忆随即对 Host 可见；`artifacts/rsi/<run-id>/candidates/<session-id>/workspace` 可能是指向受管 worktree 的链接，可先解析路径。运行中即可复制选定 skill 的普通文本文件到 Git 忽略的审阅快照，记录 session、相对路径、版本和 SHA256，复制前后核对来源未变化；来源变化则重取。不要复制认证、完整对话或无关运行数据，也不要覆盖正在写入的工作区。

回流到仓库 `skills/` 时审阅通用性、私人信息、引用、索引和版本，再按授权提交发布。Host 可以读取正在写入的文件，因此快照校验仍有必要。根目录公共 skill、Agent 可写副本、只读 `/run/input` 和各 Worker 工作区分别维护，双向内容不会自动合并。热加载部署方输入也不会自动覆盖 Agent 的 skill 副本和发现索引。需要应用到当前 Agent 时，经批准显式交付选定内容并通知 Agent；纯文本重新读取可采用，工具代码需检查实际重载机制。正式保留与后继交接仍检查声明的文件清单。

## 需要维护版本的内容

| 源文件 | Agent 可读位置 |
| --- | --- |
| `agent-inputs/SOUL.md` | `/run/input/SOUL.md`，长期行为原则 |
| `agent-inputs/<phase>/goal.md` | `/run/input/goal.md` |
| `agent-inputs/<phase>/acceptance.md` | `/run/input/acceptance.md` |
| `agent-inputs/phase-one*/instructions.md` | `/run/input/phase-instructions.md`，第一阶段专项任务协议；新第二阶段无此层 |
| `agent-inputs/shared/protocol.md` | `/run/input/protocol.md`，仅第二阶段选择加载的评估协议；shared 目录不代表通用注入 |
| `agent-inputs/shared/bootstrap.yaml` | `/run/input/bootstrap.yaml`，初始 mini 的 Prompt 与启动配置 |
| `agent-inputs/shared/execution.md` | `/run/input/execution.md` |
| `agent-inputs/shared/readiness-task.md` | `/run/input/readiness-task.md`，部署验证任务 |
| `agent-inputs/shared/workspace.md` | `/run/input/workspace-instructions.md`，构建工作区的 `harness/AGENTS.md` |

`<phase>` 为 `phase-one` 或 `phase-two`。两个阶段的目标、验收和 Prompt 对称维护；每个输入清单只包含一个阶段，并共用 agent-inputs/SOUL.md 长期原则。阶段一不加载阶段二文件，阶段二不加载阶段一 Prompt，不额外叠加重复的 common Prompt。启动参数与工作区说明共用固定接口，不包含阶段验收目标。

例外是显式 `--phase phase-one --task-profile web-extension`：加载 `agent-inputs/phase-one-web/{goal,acceptance}.md` 和 `agent-inputs/phase-one-web/instructions.md`，映射到相同 `/run/input` 文件名，三者正文直接注入主 Agent／接任候选，execution 保持可读。它不叠加普通第一阶段 A–D Prompt 或第二阶段 protocol；普通冷启动仍使用 `standard` profile 的原 A–D 输入。E-only 验收只有 `## E.` 能力项，不把继承元数据解析为新的 A–D pass。

Markdown 在标题之后写 `Version: 0.1.0`；YAML 顶层写 `version: 0.1.0`。这些版本独立维护。任何内容修改都需要递增：兼容性文字修正递增 patch，例如 `0.1.0 → 0.1.1`；能力、协议或验收变化通常递增 minor；不兼容重构递增 major。版本号与 SHA256 同时记录。同一 run 见过的同名同版本文件若内容变化，加载会被拒绝，须先修正版本号。

新增直接发给 Agent 的文件时，添加明确版本，并将其加入 `src/a3_research_agent/host/inputs/versions.py` 的输入清单。专项 Harness 自己生成的任务、记忆和提示词由 Agent 管理，本规则约束部署方发布的输入。

## 启动与恢复记录

新运行默认 `phase-one`，使用 `--phase phase-two --run-harness <明确来源>` 选择第二阶段。`--resume-from` 自动读取原 run 的阶段，显式指定冲突阶段会拒绝；跨阶段使用新 run。旧记录没有阶段字段时归属阶段一。热加载只更新当前阶段的文件。

联网补充只接受第一阶段明确保留结果，以 `--phase phase-one --task-profile web-extension --run-harness <第一阶段run目录>` 新建运行。`startup.json` 与 input manifest 保存 `task_profile`，缺省为 `standard`；恢复沿用原 profile，冲突选择拒绝，热加载也只更新该 profile。不要 resume 已 self-stop 的旧 run 来补 E。新副本初始化新上下文与 checkpoint，源保留快照不改；同 run resume 保留本轮上下文。`task_context.inherited_acceptance` 记录 A–D 的 `historical-not-recertified`、source_run 与 source_version，仅为来源元数据，不能证明新验收通过或修复原归档缺口。

web-extension 的新副本将已知旧任务状态归档至 `inherited-task-state/`，记录原文件内容摘要与来源；活动 `task.txt` 改为 E 目标，checkpoint 初始化新上下文，append-only trajectory 从空轨迹开始，旧 progress 完成状态只作继承记录，`run-outcome.json` 改为 continue 并要求新 E 证据，防止旧任务／终态或旧大轨迹污染新任务。`task_context.retired_terminal` 仍留存原终态摘要与来源。源 retained 和只读历史不变，同 run resume 不执行这种新任务初始化。继承的长期记忆不是待清空的旧任务状态。

具体归档只处理已存在的 task.txt、trajectory.jsonl、progress.md、run-outcome.json，不凭空创建缺失文件；`task_context.initialized_task_files` 逐文件记录 source_sha256、archive_path、source_run、source_version。旧 progress 原文保留，活动 progress 标记 E pending／历史未重新认证。归档路径或祖先／后代冲突时失败关闭，不覆盖材料。

可选 `--initial-feedback-file` 只用于已批准的新建第一阶段 web-extension 或 skills-extension retained run 输入，不能与 resume 或旧 `--feedback-file` 混用。读取非空 UTF-8、最多 64 KiB 的文件，在 fresh checkpoint 的目标消息后加入第二个 user message，并在新副本写 `initial-feedback.md`、在 task_context 留存摘要／字节数／来源。省略时仍只有目标消息，不自动附加固定或私有反馈。它不是动态 Host feedback 登记，不改既有运行反馈、热加载及其审批规则。

`startup.json` 和 `inputs/input-manifest.json` 保存 `phase`。`inputs/input-manifest.json` 列出整套输入的快照编号 revision、发布尝试序号 generation，以及每个文件的源路径、语义版本和 SHA256。revision 定位一组确定的文件；generation 在每次发布尝试时增加，更新失败回到旧快照也会增加，让读取程序知道期间发生过更新。两者均由 Host 自动维护，开发者维护的是各文件的语义版本。

发布开始时所有挂载先标记 status=updating（更新中），完成后才标记 active（可读取）。每次启动或更新的完整内容保存在 `input-revisions/revision-*/`；`input-events.jsonl` 保存切换记录。原始 `startup.json` 保持不变，恢复事件附带当前清单写入 `resume-events.jsonl`。旧运行首次升级时，原输入另存历史副本。

普通挂载与主会话的私有输入挂载同步发布。主 Agent 与接任候选每次模型请求都获得 SOUL、目标和验收正文；第一阶段另提供专项 Prompt，第二阶段另提供 protocol.md；普通测试 worker 的任务与 Prompt 继续由主 Agent 提供。用量日志记录请求采用的 input_revision、各文件版本与摘要。初始 mini 刷新部署方 system 消息，保留已有用户消息、模型回复和工具结果。

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

## 第二阶段核心输入与历史兼容

新 preset 第二阶段只直接注入 SOUL、goal、acceptance、protocol；execution 等按 manifest 挂载可读。第一阶段（包括 G 的完整自定义输入）仍要求 phase-instructions。模型主身份/候选/普通 worker 的分工不变。

显式恢复旧 preset 时沿用原有“当前仓库输入＋原历史”语义：旧 phase-instructions 保存在历史 revision/legacy，活动挂载和后续模型核心输入不再包含已删除层；不会仅因更新仓库就改动未恢复或正在运行的 run。旧 custom 无论是否含该可选层，都按原冻结清单校验和恢复，不从仓库补入或删除文件。

具体 run 的来源、任务切换、人工恢复/修复与旧终态说明放入当次获批的 human feedback，不写成每次请求重复的全局校准文案。不预造尚未启动的第二阶段通知。当前 G 使用其已冻结输入，不因本次模板整理变化。


## 接口按需读取与不变输入

`activate()` 为每个新 revision 的 manifest 生成 `execution_index`，索引对象包含执行文档的 SHA256、字符总数、H2 章节的标题和字符范围。索引最多 64 节，章节列表编码最多约 6000 UTF-8 字节；长标题只缩短显示，不影响范围，超出部分用 `truncated/next_offset` 指向原文。执行文档原字节、源文件摘要及所有科研标准不改变。旧快照不回写；恢复激活新 revision 时生成索引。

模型固定指令仍包含完整 SOUL、阶段目标、验收及适用 protocol，详细执行接口仍按需读取。新提示明确同一已核验版本不需重复读取，跨文件读取的一致性要求继续有效。该改变不保证模型一定避免重读，也不代表固定科研正文已减少。


## 等待中的只读通知

`GET /wait/signals` 使用已有候选身份鉴权，按 owner 返回稳定游标、该身份反馈的数量/最新 ID、自己的终态 analyzer 通知 ID、输入 revision/generation/status 和是否仍为主身份。不返回正文、私有路径或凭据；读取不确认消费，不触发模型或分析。反馈已转发、分析已回应等确认变化不单独改变游标，避免自行制造唤醒；新终态通知仍会改变它。

恢复 Harness 的等待工具每约 2 秒检查信号，worker/job 每约 10 秒检查状态。每个等待进程最多约 1800 次信号 GET/小时，每个受监控目标约 360 次状态 GET/小时，另有短请求与日志成本，无模型 token 成本。1/2 秒的信号/状态请求超时使通知不会被一轮多个慢作业饿死；延迟仍包括在途请求、OS 调度和 Host 可用性。请求失败返回观察错误，不能当作没有事件。

游标从已配对 wait call/result 读取，与既有 journal/checkpoint 一起恢复，不新增服务端消费游标。等待结束边界不会清空新事件；同一批多个 wait 均使用模型决策前游标。历史压缩可能使旧游标不再就地可见，此时对仍待回应的分析建议保守重送。该设计是至少一次的保守通知，不提供退出后自动重启或恰好一次处理；主 Agent 仍决定如何回应建议。
