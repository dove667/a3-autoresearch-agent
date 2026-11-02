# Agent 输入的版本与加载

Version: 0.3.0

本文件面向维护输入的部署方；运行监督与审批见 [操作者文档](operator-supervision.md)。目标、验收、Prompt 和执行说明由部署方维护。新启动、`--resume-from` 和 `--run-harness` 都采用启动时仓库里的当前文件，包括未提交的工作区内容。恢复保留原 run、身份、生成的 Harness 和模型对话历史；部署方 system Prompt 与所选阶段协议更新为当前版本。

## 文档职责与读者

| 文档 | 主要读者与职责 | 不承担的职责 |
| --- | --- | --- |
| `evals/<phase>/goal.md` | Agent：研究目标、范围、什么不算收益 | 操作者审批、接口操作手册 |
| `evals/<phase>/acceptance.md` | Agent / 复核者：完成条件、证据与终态交付 | 将人工建议当通过标签 |
| `evals/phase-two/protocol.md` | 第二阶段 Agent / 复核者：固定科学比较与归因规则 | 外界调度审批 |
| `configs/prompts/<phase>.md` | 主 Agent / 接任候选：行动、角色、记忆、收到反馈后的处理 | 审核外界批准、教授 operator CLI |
| `runtime/local/README.md` → `execution.md` | Agent：真实可调用 API、状态、权限和生效技术事实 | 操作者 CLI 教程 |
| 本文件 / [教程](tutorial.md) | 维护者 / 用户：输入装载、版本、使用入口 | 新增科学验收门槛 |
| [operator-supervision.md](operator-supervision.md) | 用户 / 父助手 / 操作者：低频弱监督、每次干预的批准与执行核对 | 作为 Agent input 或替 Agent 做验收 |

读取路径保持不变：阶段 Prompt + goal + acceptance（第二阶段加 protocol）直接进入主身份/接任候选模型请求；execution 等其余输入保持 `/run/input` 可读。不是仅给模型链接而拿走规则正文。普通 worker 仍由主 Agent 提供任务。`docs/` 下操作者文档不加入 `runtime/local/input_versions.py` 清单，也不注入 Agent 请求；本次职责重构不改变装载链或运行行为。

## 需要维护版本的内容

| 源文件 | Agent 可读位置 |
| --- | --- |
| `evals/<phase>/goal.md` | `/run/input/goal.md` |
| `evals/<phase>/acceptance.md` | `/run/input/acceptance.md` |
| `configs/prompts/<phase>.md` | `/run/input/phase-instructions.md`，所选阶段的完整主 Agent 协议 |
| `evals/phase-two/protocol.md` | `/run/input/protocol.md`，仅第二阶段的评估协议 |
| `configs/rsi-bootstrap.yaml` | `/run/input/bootstrap.yaml`，初始 mini 的 Prompt 与启动配置 |
| `runtime/local/README.md` | `/run/input/execution.md` |
| `runtime/local/readiness-task.md` | `/run/input/readiness-task.md`，部署验证任务 |
| `configs/prompts/workspace.md` | `/run/input/workspace-instructions.md`，构建工作区的 `harness/AGENTS.md` |

`<phase>` 为 `phase-one` 或 `phase-two`。两个阶段的目标、验收和 Prompt 对称维护；每个输入清单只包含一个阶段。阶段一不加载阶段二文件，阶段二不加载阶段一 Prompt，也不叠加 common Prompt。启动参数与工作区说明共用固定接口，不包含阶段验收目标。

Markdown 在标题之后写 `Version: 0.1.0`；YAML 顶层写 `version: 0.1.0`。这些版本独立维护。任何内容修改都需要递增：兼容性文字修正递增 patch，例如 `0.1.0 → 0.1.1`；能力、协议或验收变化通常递增 minor；不兼容重构递增 major。版本号与 SHA256 同时记录。同一 run 见过的同名同版本文件若内容变化，加载会被拒绝，须先修正版本号。

新增直接发给 Agent 的文件时，添加明确版本，并将其加入 `runtime/local/input_versions.py` 的输入清单。专项 Harness 自己生成的任务、记忆和提示词由 Agent 管理，本规则约束部署方发布的输入。

## 启动与恢复记录

新运行默认 `phase-one`，使用 `--phase phase-two --run-harness <明确来源>` 选择第二阶段。`--resume-from` 自动读取原 run 的阶段，显式指定冲突阶段会拒绝；跨阶段使用新 run。旧记录没有阶段字段时归属阶段一。热加载只更新当前阶段的文件。

`startup.json` 和 `inputs/input-manifest.json` 保存 `phase`。`inputs/input-manifest.json` 列出整套输入的快照编号 revision、发布尝试序号 generation，以及每个文件的源路径、语义版本和 SHA256。revision 定位一组确定的文件；generation 在每次发布尝试时增加，更新失败回到旧快照也会增加，让读取程序知道期间发生过更新。两者均由 Host 自动维护，开发者维护的是各文件的语义版本。

发布开始时所有挂载先标记 status=updating（更新中），完成后才标记 active（可读取）。每次启动或更新的完整内容保存在 `input-revisions/revision-*/`；`input-events.jsonl` 保存切换记录。原始 `startup.json` 保持不变，恢复事件附带当前清单写入 `resume-events.jsonl`。旧运行首次升级时，原输入另存历史副本。

普通挂载与主会话的私有输入挂载同步发布。主 Agent 与接任候选每次模型请求都获得当前阶段 Prompt、目标和验收正文；第二阶段还提供 protocol.md；普通测试 worker 的任务与 Prompt 继续由主 Agent 提供。用量日志记录请求采用的 input_revision、各文件版本与摘要。初始 mini 刷新部署方 system 消息，保留已有用户消息、模型回复和工具结果。

全部 worker 停止后，Host 先关闭更新入口，再按最终生效的验收版本作结构检查，并把该版本清单写入 outcome。历史结果保留各自产生时的版本；变更验收后仅按影响范围判断相关已有结论是否适用，不将其扩为全历史重审。各代交权前核查自己的实验闭环，后继继承已确认结论及证据引用，不承担前任过去的验收责任；最终汇总仍须如实保留未完成与未知。

## 运行期间热加载

编辑文件不会自动发布。每次热加载或中断前须用户明确批准；操作步骤唯一维护于 [操作者文档](operator-supervision.md#热加载版本化输入)。Agent 令牌不能调用控制入口。

已发出的请求按原版本返回，下次请求使用新版本。多文件读取前后 manifest.status 必须为 active，revision 与 generation 相同；updating 时等待，标识变化后重读。热加载只更新文件与 Prompt，不重建对象、不换模型或镜像。更新不启动额外 Agent、不替 Agent 选择实验、不生成验收结论。

## 显式人类反馈

恢复时与运行中均可显式登记反馈，完整操作和批准范围见 [操作者文档](operator-supervision.md#恢复时加入人类反馈)。反馈不是目标、验收或计分文件的替换；固定 0.1.0 封装协议生成独立 user message，以反馈 ID 和 SHA256 追踪。源文件可覆盖，无需用户手动改版本；封装协议变化需递增实现中的 PROTOCOL_VERSION 并同步文档。

副本、登记/定位/成功转发事件与状态保存在 `.host/human-feedback/`，不自动监视源文件。同一身份最近同内容反馈去重；反馈只绑定登记时主身份，不广播到 worker 或后继。有效历史前缀摘要定位注入位置，工具调用与结果保持配对，重建上下文时记录重新定位。模型用量 human_feedback_ids 标识该请求携带的反馈；forwarded 不表示 Agent 已采纳。没有反馈时原请求路径不变。Agent 的范围核查、决定及影响留档见各阶段 Prompt，外界批准流程只由操作者承担。
