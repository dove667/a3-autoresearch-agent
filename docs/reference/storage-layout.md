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
├── memory/                由 memory 工具管理的长期经验，Git 忽略
├── trajectories/          模型执行与恢复记录，Git 忽略
├── runs/                  Host 调试与维护测试原始输出，Git 忽略
├── .runtime/              Host 状态、技能修改历史和源码缓存，Git 忽略
├── web-cache/             共享网页缓存，Git 忽略
├── reports/               用于展示的网页工程，Git 忽略
├── archive/               历史提示词原文
├── tests/                 实现与打包检查
├── docs/                  指南、机制与历史设计
├── third_party/           固定版本的第三方 submodule
├── start-autoresearch.sh   启动入口
├── .env.example           公共配置模板
└── .env                   本机配置，Git 忽略
```

## 源码边界

`harness/` 实现模型决策循环、工具参数校验、研究文件操作、计划、上下文和知识工具。Agent 通过文件工具准备代码，通过远程作业执行研究；工具集不提供本地 Bash 或 Python 命令执行。

`host/launch.py` 装配服务，`host/gateway/` 负责身份、认证、模型和作业转发。`host/sessions/processes.py` 管理本地原生进程，`host/sessions/workspace.py` 提供受限文件访问；`host/runs/` 和 `host/releases.py` 管理恢复与发布事务。`host/source_snapshots.py` 准备只读源码，`host/remote_client.py` 封装 SSH 调用。

`execution/controller/` 使用标准库管理远程作业；`execution/scheduling/` 负责资源准入，`execution/images/` 保存远程容器构建资源。所有 Python 导入使用 `a3_research_agent.*`。

`harness/history.py` 检索原始执行历史，`harness/long_term_memory.py` 管理主动长期经验，`harness/operational_state.py` 记录读取和操作进度。`path_safety.py` 提供路径校验，`harness_payload.py` 声明发布源码。

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
│   ├── research-plan.json 研究路线、里程碑与作业引用
│   ├── continuation.json 程序生成的研究文件清单
│   ├── resume.json       程序生成的恢复清单
│   ├── run-outcome.json  受控保存的研究状态
│   ├── checkpoint.json    当前模型上下文
│   ├── pending-calls.json 工具调用 journal
│   ├── history.jsonl      原始模型消息与工具结果
│   ├── trajectory.jsonl   模型循环与请求轨迹
│   ├── operational-state.json
│   ├── compaction-*.json  压缩状态与事务
│   ├── preflight/         预检回执
│   ├── context-history/   摘要与工具结果引用的原文
│   └── .autorun-spill/    大工具结果归档
├── inputs/                当前三个 prompt 的快照
├── input-revisions/       不可变输入版本
├── .host/                 私有控制状态、锁与发布事务
├── sessions/              会话登记与共享 workspace 链接
├── source-code/           历史源码归档（如有）
├── startup.json           原始发布输入与启动选择
├── model-usage.jsonl      模型原始请求、返回与用量
└── …                      输入事件、可选知识快照与恢复记录
```

所有会话、恢复和 Harness 升级共用根目录 `workspace/`。一次只有一个研究进程使用它。普通文件工具在这里自由读写研究材料；输入目录、A3 参考源码和当前 run 的 `conversation/` 只读。根 `skills/` 与 `memory/` 通过专用工具访问。

Host 用 `A3_STATE_ROOT` 指定当前 run 的 `conversation/`，并在 `runtime_paths.state` 提供实际路径。`harness/state_paths.py` 统一定位 checkpoint、journal、压缩事务和操作台账；恢复继续读取原状态。`context-history/...`、`.autorun-spill/...` 等逻辑相对来源引用以该路径为基准，原文与内容哈希保持稳定，可通过普通文件工具只读核对。

研究计划内容由 Agent 编写，通过 `plan(action="show")` 查看、`plan(action="update", revision=..., ...)` 修改；Harness 将它保存在 `runtime_paths.state/research-plan.json`，即当前 trajectory 的 `conversation/research-plan.json`。其中记录目标、步骤、依赖、进展和作业引用。Host 管理其持久化与恢复。`progress` 保存操作进展；`progress(action="outcome", status="continue"|"blocked", reason=..., external_blocker=...)` 记录研究状态。恢复与接续清单由程序生成，`files` 只列出工作区研究文件。

`memory` 保存 Agent 主动提炼的跨会话经验；`recall` 检索原始消息和工具观测；`skills` 管理可复用方法及附件。普通 `read`、`write`、`edit` 不直接接触根技能库和长期记忆；当前会话状态允许 `read`、`list`、`search_files` 只读访问。

源码放在 `workspace/scripts/<模型或方法>/<可选研究系列>/<用途>/`，例如 `act/p142/training/`、`act/baseline/evaluation/`、`dagger/validation/`。研究系列层按需设置，按 data、training、evaluation、validation 等实际用途分类；参数变化通过配置或命令传入，复用同一入口。

实验配置与产物集中到 `workspace/experiments/<日期-实验名>/`，需要时创建 results、figures、videos、analysis 子目录。配置记录模型与数据版本、超参数、seed、评测条件，结果关联原始 job/export ID、源码路径与哈希。大文件留在执行端，本地保存引用。历史源码快照随对应 trajectory 保存。

retained 知识快照保存 `scripts/` 与 `experiments/` 中的 UTF-8 文本；实验二进制留在共享 workspace，仅在 `retained.json` 中登记路径、SHA256 与大小。迁移到另一项目或执行环境时，通过 exports 交接所需二进制资产。

## 调试、发布与远程数据

| 路径 | 用途 |
| --- | --- |
| `runs/<开始时间>/` | Host 控制台、Gateway 与会话诊断日志 |
| `runs/checks/<检查主题>/` | 测试和维护验证的原始输出 |
| `.runtime/skill-revisions/` | 技能修改前的完整包历史 |
| `.runtime/night-window/` | 夜间调度与发布选择 |
| `.runtime/published-harness/v2/<commit>/` | 固定 Harness 源码缓存 |
| `.runtime/project-job-index.json` | 历史远程作业索引 |
| `web-cache/` | 可复用的公共网页内容 |
| `reports/` | 面向展示的网页工程 |
| `remote-runtime/` | 执行端示例根目录：作业、租约、预算与导出授权 |
| `models/`、`datasets/`、`model-cache/` | 由执行端配置的权重、数据和缓存 |

维护测试输出集中到 `runs/checks/`，持久工作约定更新本地 `AGENTS.md`。部署参数由 `.env` 配置，见[环境配置](../guides/setup.md)。

所有 Controller 共用配置的执行端资源根目录，源码升级继续使用原 GPU 调度锁和作业记录。大文件与只读 exports 留在执行端。原地升级边界保存在 `.host/release-upgrades/`，事件保存在 `.host/release-events.jsonl`。
