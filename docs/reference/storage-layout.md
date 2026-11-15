# 目录职责与产物管理

固定实现进入 Git；研究工作区、执行轨迹、实验记录、部署配置和展示网页留在本地。

```text
.
├── src/a3_research_agent/
│   ├── harness/           模型循环、工具、上下文、记忆与 skills
│   ├── host/              发布、Gateway、进程、输入与运行记录
│   ├── execution/         Controller、作业、GPU 调度和远程镜像
│   ├── cli/               启动和操作者命令
│   ├── harness_payload.py 发布源码载荷清单
│   └── path_safety.py     文件路径与访问范围校验
├── prompt/                平铺的 SOUL、执行约定与工作区说明
├── archive/               历史阶段提示词
├── skills/                经审核的完整研究方法包
├── tests/                 固定实现的回归与打包检查
├── scripts/               薄命令入口
├── docs/                  安装、使用、架构与接口说明
├── third_party/           固定版本的第三方 submodule
├── workspace/             唯一研究工作区，Git 忽略
├── memory/                主动长期经验及证据引用，Git 忽略
├── trajectories/          按开始时间归档的模型执行记录，Git 忽略
├── experiments/           已整理的实验结果与分析证据，Git 忽略
├── runs/                  调试日志、维护验证与构建记录，Git 忽略
├── .runtime/              Host 状态与可重建源码缓存，Git 忽略
├── web-cache/             共享网页内容缓存，Git 忽略
├── reports/               面向展示的网页报告工程，Git 忽略
├── .env.example           公共部署模板
└── .env                   本机配置，Git 忽略
```

## 源码边界

`host/launch.py` 装配并启动服务，`host/gateway/` 按模型、作业、会话、网络与操作者控制分组，所有路由共用同一身份注册表、锁和 checkpoint。

`host/sessions/processes.py` 只处理本地原生进程生命周期；`host/sessions/workspace.py` 提供当前工作区和受限文件操作。`host/runs/` 管理运行状态、checkpoint 配对、恢复与保留结果；`host/releases.py` 处理暂停期间的原地发布事务。

`execution/controller/` 使用标准库实现作业入口，`execution/scheduling/` 负责资源准入，`execution/images/` 保存远程容器构建资源。远程 Controller 从 `src/a3_research_agent/execution/controller/__main__.py` 或安装后的命令运行。所有 Python 导入使用 `a3_research_agent.*`。

根目录 `skills/` 的每个方法包通过 `SKILL.md` 和 `assets/catalog-entry.json` 声明正文、附件与版本。Host 发现所有完整包并交付部署输入；工作区 `skills/` 保存 Agent 自写内容与局部修订，安装新参考版本时保留这些内容。

Harness 通过 Gateway 请求模型、计算和外部网络。工具参数校验、文件预处理和结果展示归属 `harness/`；身份、认证和转发归属 Host；计算资源与作业状态归属 Controller。

`harness/history.py` 检索原始执行历史，`harness/long_term_memory.py` 管理主动长期经验，`harness/operational_state.py` 记录读取进度和操作状态。`path_safety.py` 提供共用路径校验，`harness_payload.py` 声明发布需要的源码文件。

## 研究与运行目录

```text
workspace/
├── research/              Agent 编写的研究脚本与分析
├── results/               Agent 本地研究输出，A3_RUN_DIR
├── skills/                工作区 skills
├── context-history/       上下文摘要绑定的原文快照与工具归档
├── history.jsonl          原始模型消息与工具结果
├── research-plan.json     研究路线、里程碑与作业引用
├── checkpoint.json        模型上下文与恢复状态
└── …                      工具 journal、轨迹、计划与研究文件

memory/
└── entries.jsonl          主动长期经验、适用条件、修正与来源

trajectories/<开始时间>/
├── startup.json           原始发布输入与启动选择
├── inputs/                当前部署方输入
├── input-revisions/       输入的完整历史版本
├── input-events.jsonl     输入版本事件
├── sessions/<session-id>/ 原生会话记录及 workspace 链接
├── .host/                 私有状态、锁与发布事务
├── fixtures/              隔离检查记录
├── retained-knowledge/    可选的不可变研究知识快照
├── retained.json          固定发布源码与选定知识引用
├── supervision.json       Host 监督状态
└── model-usage.jsonl       模型原始请求、返回与调用记录
```

所有未来会话、恢复和 Harness 升级共用根目录 `workspace/` 与 `memory/`。Host 独立保存 run、研究身份与远程作业的归属映射，工作区保持单个活动研究进程使用。

`memory/entries.jsonl` 保存 Agent 主动选择、提炼和修正的长期经验，通过 `memory` 工具按条件检索；`workspace/history.jsonl` 保留原始消息与工具观测，通过 `recall` 定点核对来源。`workspace/skills/` 保存可复用的方法。恢复时继续读取已有文件。

工作区中的 `context-history/` 由上下文管理器在需要保存原文快照或大工具结果时写入。源码仓库根目录无需同名空目录。

`runs/<开始时间>/` 保存 Host 标准输出、控制台、会话生命周期和 Gateway 诊断日志；维护验证也按任务放在 `runs/`。科学结果、数据分析与实验对照整理到 `experiments/<日期_主题>/`，并保留原始作业和产物引用。

运行目录按 Host 本地开始时间命名为 `YYYY-MM-DD_HH-MM-SS-ffffff`，重名时原子追加序号。`trajectories/latest-run.txt` 只帮助定位最近运行。恢复显式指定原 run，首次启动显式指定已发布 Harness 提交。

`.host/research-workspace.json` 保存固定的 `run_id` 和 Harness 提交，`sessions/<session-id>/workspace` 指向根目录共享工作区。Host 用原始身份查询和管理远程作业，checkpoint、长期记忆和研究代码持续保留。

研究工作区独立于 Harness 源码缓存。`.runtime/published-harness/v2/<commit>/` 保存固定发布源码；原地升级继续使用同一研究目录。升级边界保存在 `.host/release-upgrades/`，事件保存在 `.host/release-events.jsonl`。私有状态、认证和锁不作为研究产物分享。

## 其他产物

| 路径 | 用途 |
| --- | --- |
| `workspace/` | 当前研究代码、上下文、计划、自写 skills 与原始历史 |
| `memory/entries.jsonl` | Agent 主动积累的长期经验 |
| `runs/` | 调试日志、测试、维护验证、构建与迁移记录 |
| `.runtime/night-window/` | 夜间调度与发布选择 |
| `.runtime/published-harness/v2/<commit>/` | 可重建的固定 Harness 源码缓存 |
| `.runtime/project-job-index.json` | 项目历史远程作业索引 |
| `web-cache/` | 可复用的网页内容与请求缓存 |
| `remote-runtime/` | 执行端示例根目录：作业、租约、显存预算与导出授权 |
| `models/`、`datasets/`、`model-cache/` | 可选权重、数据及下载缓存，位置由执行端配置 |
| `experiments/<日期_主题>/` | 实验结果、配置、分析与证据引用 |
| `reports/` | 面向展示的网页及其工程文件 |

所有 Controller 必须共用配置的执行端资源根目录；切换源码版本不创建另一套 GPU 锁。远程数据和导出快照留在执行端，`retained.json` 只保存选定引用。

历史档案保留用于复核。旧 builder、本地容器和旧生成 Harness 格式不进入当前启动路径。需要继续的研究由维护者离线整理为当前原生 schema，并保留整理前的记录和校验清单。

部署参数从 `.env.example` 复制配置，读取规则见[环境配置](../guides/setup.md)。Agent 输入版本与文件清单见[输入管理](agent-inputs.md)。实验与展示目录保持 Git 忽略，公开文档只引用仓库内可获得的说明。
