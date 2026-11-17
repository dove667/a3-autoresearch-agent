# A3 Research Agent 使用教程

本地 Host 管理模型连接、权限和运行记录，已发布的 Harness 在项目 Python 环境中作为原生进程执行。训练、采集、仿真和评估通过 SSH 交给远程 Controller。

## 安装与配置

```bash
git submodule update --init --recursive
uv sync --python 3.11
cp .env.example .env
```

在 `.env` 配置模型服务、各模型的实际上下文窗口、A3 源码、SSH 目标、远程 Controller、资产和资源预算。凭据由 Host 保管。远程受信任进程后端的准备方式见[进程后端](../reference/remote-process.md)；模型与依赖见[环境配置](setup.md)。常规本地运行无需 Docker。

```bash
bash start-autoresearch.sh --check
```

检查会核对配置、A3 源码与远程 Controller 健康响应。模型调用、真实训练和科学评估分别由对应运行完成。

## 首次启动已发布的 Harness

选定 `origin/main` 可达的完整 Git 提交 SHA：

```bash
git fetch origin
bash start-autoresearch.sh --harness-commit <已发布提交SHA>
```

Host 验证固定提交，将 Harness 缓存在 `.runtime/published-harness/v2/<commit>/`，研究文件使用项目根目录唯一的 `workspace/`，技能使用根 `skills/`，主动经验由 `memory` 工具管理。模型通过具体工具编写研究脚本、提交作业、读取结果与记录经验。研究源码和产物留在该工作区，运行中的 Harness 源码从已发布版本加载。

每天启动一个新的 **run**，保存当天的主身份、空对话起点与执行轨迹。所有运行共用 workspace、plan、skills 和 memory，Agent 先读共享计划并核对已有 job。手动恢复可以继续指定 run 的对话和在途调用。一次 **job** 是远程采集、训练或评估命令，拥有持久编号和实际执行状态。

原始 mini-SWE-agent 可独立用于开发：

```bash
uv run mini
```

旧生成式 Harness 的本地执行入口已退役。其原始轨迹与研究材料继续保留；整理为经过审查的发布源码后使用上述入口。

## 恢复原研究

```bash
bash start-autoresearch.sh --resume-from "trajectories/<开始时间>"
```

恢复使用同一个 run 和主身份，接续 `trajectories/<开始时间>/conversation/` 中的运行状态、对话、checkpoint 和调用 journal，继续 `workspace/` 的研究文件、根 `skills/` 和长期经验，并读取项目顶层 `plan/current.json` 的最新研究计划。原生进程中断时保留工具 journal；已保存的结果在恢复时重新交付，结果未知的调用先检查实际状态。

远程作业在本地 Agent 暂停期间可以继续运行。恢复后通过原 job ID 接回状态与产物，按既有结果推进研究。普通恢复沿用原模型与适配器；明确维护发布时可更新部署配置并记录前值。

原地发布升级要求 run 已暂停，使用当前原生记录格式。历史运行需先离线整理并保留原始记录。

## 在同一工作区更新 Harness

维护修改通过回归与审查后，将源码推送到 `origin/main`，下一次启动选择新提交：

```bash
bash start-autoresearch.sh --harness-commit <新发布提交SHA>
```

新运行从空模型上下文开始，继续使用原 `workspace/`、`plan/current.json`、`skills/` 和 `memory/`。无需把旧代码、技能和实验材料重新复制进 trajectory。需要接续原对话时，明确指定旧运行使用手动恢复；每日研究默认新建运行。

## 夜间窗口与防休眠

```bash
bash start-autoresearch.sh --night-window --harness-commit <已发布提交SHA>
```

窗口为北京时间 21:00 到次日 `A3_NIGHT_WINDOW_END_HOUR` 指定的小时（默认 9，即 09:00；设为 7 则 07:00 结束）。每个窗口创建独立日期目录和新的模型对话，复用共享工作区、研究计划、技能和长期记忆。到点保存记录并结束 Agent，远程 Controller 继续管理已提交作业；下一窗口的新 Agent 先核对计划与原 job ID。窗口内 Host 自行退出时启动器返回退出结果；若 Host 在写入 run 状态前失败（例如 21:00 启动瞬间断网），启动器按 15/30/60/120/300 秒退避重试、清掉空的预留目录，并把启动命令与 SSH 失败原因写进启动日志，直到窗口结束。需要原对话时显式使用 `--resume-from` 手动恢复。更换夜间 Harness 版本时，用新的提交 SHA 重新启动夜间启动器。

macOS 脚本自动使用 `caffeinate -is`，运行期间防止空闲休眠，退出后释放。保持供电、开盖和网络连接。需要关闭终端后继续运行时，可显式后台启动：

```bash
mkdir -p runs/night-window
nohup bash start-autoresearch.sh --night-window --harness-commit <已发布提交SHA> \
  > runs/night-window/launcher.log 2>&1 &
```

主机重启后重新运行夜间入口。保持单个研究进程使用共享工作区，启动日志也单独保存。

## 常用工具和作业流程

CPU作业使用 `job_submit(name="训练ACT", command=[...], attachments=[{"path":"scripts/train.py"}])`；每次提交给作业一个简短的人类可读名称；
源码和实验文本配置都可附加，工具自动计算哈希。GPU作业先用
`job_prepare(command=[...], gpu=true, attachments=[...], input_exports=[...])` 保存正式请求并执行CPU检查。
入口在 `A3_RESEARCH_PREFLIGHT=1` 时加载真实模型与数据，检查通过写入工作区
`a3-preflight.json`，内容为 `{"schema":1,"status":"passed"}`。
收到 prepared_id 和 preflight_job_id 后，调用 `job_wait(job_ids=[preflight_job_id])`；
检查完成，再用 `job_submit(prepared_id="...")` 提交保存的请求，无需重填参数。

独立且输入就绪的任务可以并行提交。`job_wait` 默认持续等待指定作业终态或通知，显式填写timeout_seconds才限时；省略job_ids时
等待程序已观察到的活动作业。`idle_wait(seconds=30)` 只等操作者或输入通知，不轮询作业。
Controller负责排队、GPU分配与生命周期。提交结果未知时先按编号查询，避免重复启动。

`job_status`、`job_list` 查询实际状态，`job_artifact` 按字符分页读取结果和哈希。
资产通过 `export_create`、`export_status`、`export_manifest`、`export_list` 管理；
`evaluation_compare` 比较已有配对结果，`vision_inspect` 检查指定的原始帧。
`search_files` 查找源码与记录，`edit` 精确修改工作区文件；覆盖现有文件的write需要当前哈希。
高层工具返回统一ok标志、失败提示和分页游标，模型无需构造HTTP方法、URL或原始body。
详细参数见[执行接口](../../prompt/execution.md)。

文件工具在 `workspace/` 自由读写，输入、A3 参考源码和当前会话状态只读；根技能库和长期记忆分别通过 `skills`、`memory` 操作。Agent 工具不提供本地 Bash 或 Python，研究脚本通过远程 jobs 执行。

采集与评估通过[episode 执行器](../reference/episode-executor.md)管理完成台账和并发。作业脚本从环境读取 `A3_OUTPUTS`、`A3_EPISODE_EXECUTOR` 及资产路径，避免沿用另一后端的路径。

研究计划按长期方向、研究路线与假设、执行步骤三层组织，由 Agent 编写，通过 `plan(action="show")` 查看、`plan(action="update", revision=..., ...)` 修改，统一保存到项目顶层 `plan/current.json`，路径由 `runtime_paths.plan` 提供。关键结果返回后，Agent 读取原始产物，通过 `plan(action="review", revision=..., id=<路线ID>, conclusion=..., evidence=[...], decision="continue"|"pause"|"complete")` 保存复盘和路线决策。每轮上下文包含计划摘要与待复盘作业，新运行、恢复和发布升级共用最新计划，先核对计划中的既有 job 并重新检查阻塞；各次运行的开始、修订和停止快照保存在对应 trajectory 的 `plan-snapshots/`。任务和研究进展只在plan维护；程序自动记录读取和作业观察，整轮状态由 `research_status` 保存到当前 `conversation/`。恢复和接续清单由程序生成，`files` 只列出工作区研究文件。

长期经验由 `memory` 保存，search使用query，read使用id，修订/归档/恢复使用当前revision；`recall`只检索当前run的原始观测。根 `skills/` 是唯一技能库，通过 `skills` 先看目录摘要，再读正文和所需附件；同一工具也负责更新正文、附件和归档。工作上下文达到压缩门槛后必须生成完整摘要，32k 等目标预算覆盖整个下一轮输入；机制见[上下文与记忆](../reference/context-and-memory.md)。

## 持续研究与暂停

长期目标直接写在 `prompt/SOUL.md`：持续提高 A3 2×5、2×10 全量评测的入盒准确率与通过率。Agent 根据证据调整假设、数据、模型和研究路线，记录模型版本、数据清单、种子与评测设置，持续比较结果。全量评测用于衡量研究进展，由操作者决定暂停和恢复。日常 Harness 维护沿用原 run 和工作区。

Agent 用 `research_status(status="continue", reason=...)` 保存整轮状态；需人工处理的外部阻塞使用 `status="blocked"`，并提供kind、required_action和实际存在的workspace证据路径evidence。Host 保存相应元数据，研究文件继续留在工作区。

## 查看研究状态

本地看板把最新 run、共享计划、远程作业、长期记忆、工具与技能分成多个页面。研究 Host 启动时自动启动看板，研究停止时一起关闭，默认地址为 <http://127.0.0.1:8787>；每 5 秒自动更新。

需要单独查看历史且没有研究运行时，可以手动启动：

```bash
uv run a3-research-dashboard --open
```

它只读本机文件，不修改研究状态。端口、状态含义和数据来源见[研究看板](dashboard.md)。

## 查找状态与产物

- `trajectories/<开始时间>/`：输入版本、Host 事件、模型用量与会话登记。
- `workspace/scripts/<模型或方法>/<可选研究系列>/<用途>/`：采集、训练、评估与审计源码。
- `workspace/experiments/<日期-实验名>/`：实验配置、结果、图表、录像与分析，关联 job/export 和源码哈希。
- `plan/current.json`：跨运行共用的最新研究计划，通过 `plan` 工具管理。
- `trajectories/<开始时间>/plan-snapshots/`：该次运行期间的计划历史快照。
- `trajectories/<开始时间>/conversation/`：接续清单、研究状态、checkpoint、journal、原始消息和压缩状态。
- `skills/`：由 `skills` 工具管理的唯一方法库。
- `memory/entries.jsonl`：Agent 主动整理的长期经验、适用条件和证据引用。
- `trajectories/<开始时间>/.host/release-upgrades/`：原地升级事务和旧状态边界。
- 执行端配置的运行根目录：持久 job 元数据、日志、输出与只读 exports。

排查时核对当前 `runtime_paths.state` 下的 `loop-failure.json`、`pending-calls.json`、Gateway 事件和对应 job 的实际状态。提交结果未知时沿返回的 job ID 查询；源码或依赖错误由 Agent 修复，Controller、认证和部署问题交给维护者。目录职责见[项目结构](../reference/storage-layout.md)。
