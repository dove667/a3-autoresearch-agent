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

恢复使用同一个 run 和主身份，接续 `trajectories/<开始时间>/conversation/` 中的运行状态、对话、checkpoint 和调用 journal，继续 `workspace/` 的研究文件、根 `skills/` 和长期经验，并读取项目顶层 `plan.json` 的最新研究计划。原生进程中断时保留工具 journal；已保存的结果在恢复时重新交付，结果未知的调用先检查实际状态。

远程作业在本地 Agent 暂停期间可以继续运行。恢复后通过原 job ID 接回状态与产物，按既有结果推进研究。普通恢复沿用原模型与适配器；明确维护发布时可更新部署配置并记录前值。

原地发布升级要求 run 已暂停，使用当前原生记录格式。历史运行需先离线整理并保留原始记录。

## 在同一工作区更新 Harness

维护修改通过回归与审查后，将源码推送到 `origin/main`，下一次启动选择新提交：

```bash
bash start-autoresearch.sh --harness-commit <新发布提交SHA>
```

新运行从空模型上下文开始，继续使用原 `workspace/`、`plan.json`、`skills/` 和 `memory/`。无需把旧代码、技能和实验材料重新复制进 trajectory。需要接续原对话时，明确指定旧运行使用手动恢复；每日研究默认新建运行。

## 夜间窗口与防休眠

```bash
bash start-autoresearch.sh --night-window --harness-commit <已发布提交SHA>
```

窗口为北京时间 21:00 到次日 09:00。每个窗口创建独立日期目录和新的模型对话，复用共享工作区、研究计划、技能和长期记忆。到点保存记录并结束 Agent，远程 Controller 继续管理已提交作业；下一窗口的新 Agent 先核对计划与原 job ID。窗口内进程自行退出时，启动器返回退出结果；需要原对话时显式使用 `--resume-from` 手动恢复。更换夜间 Harness 版本时，用新的提交 SHA 重新启动夜间启动器。

macOS 脚本自动使用 `caffeinate -is`，运行期间防止空闲休眠，退出后释放。保持供电、开盖和网络连接。需要关闭终端后继续运行时，可显式后台启动：

```bash
mkdir -p runs/night-window
nohup bash start-autoresearch.sh --night-window --harness-commit <已发布提交SHA> \
  > runs/night-window/launcher.log 2>&1 &
```

主机重启后重新运行夜间入口。保持单个研究进程使用共享工作区，启动日志也单独保存。

## 常用工具和作业流程

`job_submit` 接受命令参数数组、源码路径和资产引用。工具自动计算本地源码哈希；训练数据与权重使用只读 exports。独立且输入就绪的任务可先并行提交，再用 `wait` 等待终态或操作者通知。Controller 负责排队、GPU 分配与生命周期。

`job_status` 和 `job_list` 查询实际状态，`job_artifact` 按字符分页读取结果并返回文件哈希。`search_files` 查找源码与可读记录，`edit` 精确修改工作区研究文件。预检、技能与联网工具同样提供具体参数定义，详细契约见[执行接口](../../prompt/execution.md)。

文件工具在 `workspace/` 自由读写，输入、A3 参考源码和当前会话状态只读；根技能库和长期记忆分别通过 `skills`、`memory` 操作。Agent 工具不提供本地 Bash 或 Python，研究脚本通过远程 jobs 执行。

采集与评估通过[episode 执行器](../reference/episode-executor.md)管理完成台账和并发。作业脚本从环境读取 `A3_OUTPUTS`、`A3_EPISODE_EXECUTOR` 及资产路径，避免沿用另一后端的路径。

研究计划由 Agent 编写，通过 `plan(action="show")` 查看、`plan(action="update", revision=..., ...)` 修改，统一保存到项目顶层 `plan.json`，路径由 `runtime_paths.plan` 提供。新运行、恢复和发布升级共用最新计划，先核对计划中的既有 job；各次运行的开始、修订和停止快照保存在对应 trajectory 的 `plan-snapshots/`。操作进展与研究状态由 `progress` 保存到当前 `conversation/`。恢复和接续清单由程序生成，`files` 只列出工作区研究文件。

长期经验由 `memory` 保存；`recall` 从原始历史检索观测。根 `skills/` 是唯一技能库，通过 `skills` 先看目录摘要，再读正文和所需附件；同一工具也负责更新正文、附件和归档。工作上下文达到压缩门槛后必须生成完整摘要，32k 等目标预算覆盖整个下一轮输入；机制见[上下文与记忆](../reference/context-and-memory.md)。

## 持续研究与暂停

长期目标直接写在 `prompt/SOUL.md`：持续提高 A3 2×5、2×10 全量评测的入盒准确率与通过率。Agent 根据证据调整假设、数据、模型和研究路线，记录模型版本、数据清单、种子与评测设置，持续比较结果。全量评测用于衡量研究进展，由操作者决定暂停和恢复。日常 Harness 维护沿用原 run 和工作区。

Agent 用 `progress(action="outcome", status="continue", reason=...)` 保存继续研究的状态；需人工处理的外部阻塞使用 `status="blocked"` 并附 `external_blocker`。Host 保存相应元数据，研究文件继续留在工作区。

## 查找状态与产物

- `trajectories/<开始时间>/`：输入版本、Host 事件、模型用量与会话登记。
- `workspace/scripts/<模型或方法>/<可选研究系列>/<用途>/`：采集、训练、评估与审计源码。
- `workspace/experiments/<日期-实验名>/`：实验配置、结果、图表、录像与分析，关联 job/export 和源码哈希。
- `plan.json`：跨运行共用的最新研究计划，通过 `plan` 工具管理。
- `trajectories/<开始时间>/plan-snapshots/`：该次运行期间的计划历史快照。
- `trajectories/<开始时间>/conversation/`：接续清单、研究状态、checkpoint、journal、原始消息和压缩状态。
- `skills/`：由 `skills` 工具管理的唯一方法库。
- `memory/entries.jsonl`：Agent 主动整理的长期经验、适用条件和证据引用。
- `trajectories/<开始时间>/.host/release-upgrades/`：原地升级事务和旧状态边界。
- 执行端配置的运行根目录：持久 job 元数据、日志、输出与只读 exports。

排查时核对当前 `runtime_paths.state` 下的 `loop-failure.json`、`pending-calls.json`、Gateway 事件和对应 job 的实际状态。提交结果未知时沿返回的 job ID 查询；源码或依赖错误由 Agent 修复，Controller、认证和部署问题交给维护者。目录职责见[项目结构](../reference/storage-layout.md)。
