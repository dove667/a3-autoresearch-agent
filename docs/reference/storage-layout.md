# 目录职责与产物管理

源码和提示词进入 Git；研究文件、对话状态、长期经验、调试记录与展示网页按用途分别保存。

```text
.
├── src/a3_research_agent/
│   ├── harness/           模型循环、工具、计划、上下文与知识管理
│   ├── host/              发布、Gateway、身份、进程与恢复
│   ├── execution/         Controller、作业、GPU 调度和执行后端
│   ├── cli/               用户和操作者命令
│   ├── harness_payload.py 发布源码载荷清单
│   └── path_safety.py     文件路径与访问范围校验
├── prompt/                SOUL、执行约定与工作区说明
├── skills/                全项目唯一技能库
├── workspace/             Agent 自由读写的研究区，Git 忽略
├── memory/                由 memory 工具管理的经验修订与语义索引，Git 忽略
├── plan/                  跨运行研究计划与复盘，Git 忽略
├── trajectories/          模型执行与恢复记录，Git 忽略
├── runs/                  Host 调试与维护测试原始输出，Git 忽略
├── .runtime/              Host 状态、技能修改历史和源码缓存，Git 忽略
├── web-cache/             共享网页缓存，Git 忽略
├── reports/               用于展示的网页工程，Git 忽略
├── legacy_prompt/         停用的历史提示词原文
├── tests/                 实现与打包检查
├── docs/                  指南、机制与历史设计
├── third_party/           固定版本的第三方 submodule
├── start-autoresearch.sh   启动入口
├── .env.example           公共配置模板
└── .env                   本机配置，Git 忽略
```

## 源码边界

`harness/` 实现模型决策循环、工具参数校验、研究文件操作、计划、上下文和知识工具。Agent 通过文件工具准备代码，通过远程作业执行研究；工具集不提供本地 Bash 或 Python 命令执行。

`host/launch.py` 装配服务，`host/gateway/` 负责身份、认证、模型和作业转发。`host/sessions/processes.py` 管理本地原生进程，`host/sessions/workspace.py` 提供受限文件访问；`host/runs/` 和 `host/releases.py` 管理恢复与发布事务。`host/reference_sources.py` 准备共享只读参考源码缓存，`host/remote_client.py` 封装 SSH 调用。

`execution/controller/` 使用标准库管理远程作业；`execution/scheduling/` 负责资源准入。所有 Python 导入使用 `a3_research_agent.*`。

`harness/history.py` 检索原始执行历史，`harness/memory/` 管理主动长期经验，`harness/operational_state.py` 记录程序观察到的读取与作业状态。`path_safety.py` 提供路径校验，`harness_payload.py` 声明发布源码。

## 研究文件与机器状态

```text
workspace/
├── scripts/               按模型或方法、研究系列和用途组织的源码
│   ├── act/
│   │   ├── baseline/      独立基线系列
│   │   ├── p142/          同一研究系列的 data、training、evaluation 等入口
│   │   └── shared/        ACT 共用的资产与诊断脚本
│   └── dagger/            按来源核对、对账、验证等用途分类
└── experiments/<日期-实验名>/
    ├── config.json        模型、数据、超参数、seed 与评测设置
    ├── report.md          实验报告与结论
    ├── results/           逐回合结果、汇总、作业与源码引用
    ├── figures/           图表
    ├── videos/            录像
    └── analysis/          分析与实验结论

memory/
└── entries.jsonl          主动长期经验、适用条件、修正与来源

trajectories/<开始时间>/
├── conversation/
│   ├── continuation.json 程序生成的研究文件清单
│   ├── resume.json       程序生成的恢复清单
│   ├── run-outcome.json  受控保存的研究状态
│   ├── checkpoint.json    当前模型上下文
│   ├── pending-calls.json 工具调用 journal
│   ├── history.jsonl      原始模型消息与工具结果
│   ├── trajectory.jsonl   模型循环与请求轨迹
│   ├── operational-state.json
│   ├── research-ledger.jsonl  作业与压缩事件的有界台账，供白天分析
│   ├── summary-breaker.json   摘要熔断状态，可删除且不影响研究
│   ├── research-receipts.json 近期关键工具观察与原文索引
│   ├── compaction-*.json  压缩状态与事务
│   ├── context-failure.json 压缩等待或阻塞状态（恢复后清除）
│   ├── prepared-jobs/     保存的正式请求、CPU预检与提交回执
│   └── context-history/   摘要、校验回执与完整工具输出原文
├── plan-snapshots/        该次运行开始、修订和停止时的计划历史快照
├── inputs/                当前三个 prompt 的快照
├── input-revisions/       不可变输入版本
├── .host/                 私有控制状态、锁与发布事务
├── sessions/              会话登记与共享 workspace 链接
├── public/a3              指向共享只读参考源码的链接
├── retained-files.json    共享研究文件的路径与哈希清单
├── startup.json           原始发布输入与启动选择
├── model-usage.jsonl      模型原始请求、返回与用量
├── model-records.tar.gz   历史轮整理后的去重原始记录（如有）
└── …                      输入事件、可选知识快照与恢复记录
```

每天的新 run 共用根目录 `workspace/`、`plan/current.json`、`memory/` 和 `skills/`，以空模型上下文读取最新研究进展。一次只有一个研究进程使用它。普通文件工具在这里自由读写研究材料；输入目录、A3 参考源码和当前 run 的 `conversation/` 只读。根 `skills/` 与 `memory/` 通过专用工具访问。

Host 用 `A3_STATE_ROOT` 指定当前 run 的 `conversation/`，并在 `runtime_paths.state` 提供实际路径。`harness/state_paths.py` 统一定位 checkpoint、journal、压缩事务和操作台账；恢复继续读取原状态。`context-history/...` 来源引用以该路径为基准，原文与内容哈希保持稳定，可通过普通文件工具只读核对，也可用 recall 检索当前 run 原文。`research-receipts.json` 保存有界的近期工具观察，用于每轮刷新编号、状态和指标；旧观察保留在原始历史和输出归档中。

项目顶层 `plan/current.json` 保存跨运行共用的最新研究计划，记录长期方向、研究路线与假设、执行步骤和结果复盘，关联依赖、优先级、作业与证据。Agent 通过 `plan(action="show")` 查看、`plan(action="update", revision=..., ...)` 修改；Host 通过 `A3_PLAN_FILE` 配置绝对路径，并在 `runtime_paths.plan` 中提供该路径。普通文件工具不直接修改计划。每次运行在 `plan-snapshots/` 按 revision 与内容哈希保留开始、修订和停止时的历史快照；新运行和恢复始终读取根计划。旧 `conversation/research-plan.json` 保留为历史资料。

plan 是 Agent 维护任务与进展的唯一位置；`conversation/operational-state.json` 自动记录读取和作业状态。`research_status(status="continue"|"blocked", reason=..., kind=..., required_action=..., evidence=[...])` 保存整轮研究状态，外部阻塞使用后三项，普通继续状态只需 status 和 reason。恢复与接续清单由程序生成，`files` 只列出工作区研究文件。

`memory` 保存 Agent 主动提炼的跨会话经验；`recall` 检索当前 run 的原始消息和工具观测；`skills` 管理可复用方法及附件。普通 `read`、`write`、`edit` 不直接接触根技能库和长期记忆；当前会话状态允许 `read`、`list`、`search_files` 只读访问。

源码放在 `workspace/scripts/<模型或方法>/<可选研究系列>/<用途>/`，例如 `act/p142/training/`、`act/baseline/evaluation/`、`dagger/validation/`。研究系列层按需设置，按 data、training、evaluation、validation 等实际用途分类；参数变化通过配置或命令传入，复用同一入口。

实验配置与产物集中到 `workspace/experiments/<日期-实验名>/`，需要时创建 results、figures、videos、analysis 子目录。配置记录模型与数据版本、超参数、seed、评测条件，结果关联原始 job/export ID、源码路径与哈希。大文件留在执行端，本地保存引用。trajectory 保留模型原始行为、工具调用与返回、输入版本和计划历史；研究脚本继续由 workspace 管理，技能统一由根技能库管理。

retained 记录引用共享 workspace 中的研究文件：`retained-files.json` 保存文本路径与 SHA256，`retained.json` 关联该清单并登记二进制路径、SHA256 与大小。加载时核对实际文件；已改变或缺失的材料需要重新发布保留结果。同一 run 的恢复继续使用当前工作区。迁移到另一项目或执行环境时，通过 exports 交接所需二进制资产。

## 调试、发布与远程数据

| 路径 | 用途 |
| --- | --- |
| `runs/<开始时间>/` | Host 控制台、Gateway 与会话诊断日志 |
| `runs/checks/<检查主题>/` | 测试和维护验证的原始输出 |
| `.runtime/skill-revisions/` | 技能修改前的完整包历史 |
| `.runtime/night-window/` | 夜间调度与发布选择 |
| `.runtime/published-harness/v2/<commit>/` | 固定 Harness 源码缓存 |
| `.runtime/reference-sources/<内容摘要>/` | 按实际内容复用的只读 A3 参考源码 |
| `.runtime/project-job-index.json` | 历史远程作业索引 |
| `web-cache/` | 可复用的公共网页内容 |
| `reports/` | 面向展示的网页工程 |
| `remote-runtime/` | 执行端示例根目录：作业、租约、预算与导出授权 |
| `MODELS/`、`DATASETS/`、`model-cache/` | 由执行端配置的权重、数据和缓存 |

数据与模型存储按研究方向和来源组织。项目通用基准和资产使用 `shared/`，其余使用 `<方向>/<来源>/<资产版本>/`，例如 `act/autoresearch/` 和 `moe-act/codex/`。同一数据根保存原始采集、合并集和可复用预处理。模型根保存训练 checkpoint 及其加载所需文件。数据与模型用清单记录各自来源和版本，已发布版本保持稳定。自动研究作业通过 `A3_DATASETS`、`A3_MODELS` 读取根目录，手动实验使用相同配置。单次作业的评测和诊断仍通过原作业编号读取。

归属待核的迁移资产放在 `historical/autoresearch/`。若资产根有 `a3-migration.json`，其 `assets` 条目用 `path` 记录相对根目录的位置，有旧路径的条目还记录 `source`。Agent 通过只读 CPU Python 作业筛选这份清单，用 `job_artifact` 查看匹配结果。清单覆盖迁移时的资产，后续发布的版本从对应方向目录查找。

A3 参考源码按实际文件内容生成共享缓存；各次运行的 `public/a3` 只保存链接，缓存不携带 Git 数据库、输出或浏览器临时文件。同一内容只保存一份，源码变化产生独立版本。

历史轮可将模型和工具原文、当时输入及计划记录整理为 `model-records.tar.gz`：清单保留原相对路径与 SHA256，同一内容只保存一份原字节。旧安装代码、仓库和技能副本不再随轨迹保存；整理后的历史包供分析使用，后续研究从新 run 读取共享材料。新运行期间使用直接可读的 `conversation/` 与必要 Host 状态；结束后的历史包按分析价值保留。

`runs/` 的控制台与请求诊断按排错价值保留；`trajectories/` 按原始行为和研究经验的分析价值保留，清理前核对当前恢复及经验来源引用。维护测试输出集中到 `runs/checks/`，持久工作约定更新本地 `AGENTS.md`。部署参数由 `.env` 配置，见[环境配置](../guides/setup.md)。

所有 Controller 共用配置的执行端资源根目录，源码升级继续使用原 GPU 调度锁和作业记录。大文件与只读 exports 留在执行端。原地升级边界保存在 `.host/release-upgrades/`，事件保存在 `.host/release-events.jsonl`。

`memory/entries.jsonl` 追加保存条目版本；`memory/index.sqlite3` 是按模型与内容哈希维护的可重建向量缓存；`memory/sources.json` 连接迁入条目的历史原文。`host/knowledge_service/` 管理模型校验、私有服务和进程生命周期；`harness/memory/` 管理条目、检索、来源与受控客户端。

知识服务通过 `host/knowledge_service/` 共享一个 embedding 进程；`harness/knowledge_client.py` 是受控客户端，`harness/semantic_index.py` 提供通用向量缓存和文本归一化。技能目录与经验库分别保存，技能索引在 `.runtime/skill-index/`，索引不承担版本历史或正文存储。


高层工具定义与校验放在 `harness/tool_catalog.py`、`job_tools.py`、`host_tools.py`、
`control_tools.py` 和 `file_tools.py`。HTTP调用由固定运行层完成，模型不接触通用
Gateway入口。作业源码和文本配置统一用 attachments 引用 workspace 内文件；GPU请求
保存在当前 run 的 prepared-jobs，随后按 prepared_id 提交。计划存储使用 version2，
目标只在 direction.objective；旧目标字段在维护时一次迁移，运行时不加载旧格式。

模型工具由 `harness/tool_catalog.py` 统一注册，文件、作业、Host能力和运行控制分别在 `file_tools.py`、`job_tools.py`、`host_tools.py`、`control_tools.py` 实现；`tool_results.py` 统一模型回执。准备好的作业保存在当前 conversation/prepared-jobs/，模型使用编号提交，无需复制原请求。
