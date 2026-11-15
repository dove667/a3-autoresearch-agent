# A3 Research Agent 使用教程

本地 Host 管理模型连接、权限和运行记录，已发布的 Harness 在项目 Python 环境中作为原生进程执行。训练、采集、仿真和评估通过 SSH 交给远程 Controller。

## 安装与配置

```bash
git submodule update --init --recursive
uv sync --python 3.11
cp .env.example .env
```

在 `.env` 配置模型服务、各模型的实际上下文窗口、A3 源码、SSH 目标、远程 Controller、资产和资源预算。凭据由 Host 保管。远程受信任进程后端的准备方式见[进程后端](process-backend.md)；模型与依赖见[环境配置](setup.md)。常规本地运行无需 Docker。

```bash
bash scripts/start-rsi.sh --check
```

检查会核对配置、A3 源码与远程 Controller 健康响应。模型调用、真实训练和科学评估分别由对应运行完成。

## 首次启动已发布的 Harness

选定 `origin/main` 可达的完整 Git 提交 SHA：

```bash
git fetch origin
bash scripts/start-rsi.sh --harness-commit <已发布提交SHA> --agent-inputs-dir <任务输入目录>
```

Host 验证固定提交，将 Harness 缓存在 `artifacts/published-harness/v2/<commit>/`，并为首次研究建立工作区。模型通过具体工具编写研究脚本、提交作业、读取结果与记录经验。研究源码和产物留在该工作区，运行中的 Harness 源码从已发布版本加载。

一次 **run** 保存所选目标、主身份、轨迹和研究状态；同一 run 可以跨多次进程启动和 Harness 维护发布。一次 **job** 是远程采集、训练或评估命令，拥有持久编号和实际执行状态。

原始 mini-SWE-agent 可独立用于开发：

```bash
uv run mini
```

旧生成式 Harness 的本地执行入口已退役。其原始轨迹与研究材料继续保留；整理为经过审查的发布源码后使用上述入口。

## 恢复原研究

```bash
bash scripts/start-rsi.sh --resume-from "artifacts/rsi/<run-id>"
```

恢复使用同一个 run、主身份和工作区，继续已有对话、checkpoint、记忆、skills、计划与作业台账。原生进程中断时保留工具 journal；已保存的结果在恢复时重新交付，结果未知的调用先检查实际状态。

远程作业在本地 Agent 暂停期间可以继续运行。恢复后通过原 job ID 接回状态与产物，按既有结果推进研究。普通恢复沿用原模型与适配器；明确维护发布时可更新部署配置并记录前值。

原地发布升级要求 run 已暂停，使用当前原生记录格式。历史运行需先离线整理并保留原始记录。

## 在同一工作区更新 Harness

维护修改通过回归与审查后，将源码推送到 `origin/main`。交接文件包含 `## 已核对结论` 与 `## 待办` 两节，附需要继续使用的研究来源与作业引用。发布命令将固定提交、当前输入及交接正文冻结为一个内容摘要：

```bash
uv run python -m a3_research_agent.cli.night_release "artifacts/rsi/<锚点run>" \
  --commit <已发布提交SHA> --handoff <交接.md>

uv run python -m a3_research_agent.cli.night_release "artifacts/rsi/<锚点run>" \
  --commit <已发布提交SHA> --handoff <交接.md> --confirm <发布摘要>
```

夜间启动器在下次窗口应用该发布，然后恢复当前 run。也可以在维护窗口离线应用：

```bash
uv run python -m a3_research_agent.cli.apply_release "artifacts/rsi/<run-id>" \
  "<冻结发布目录>" --digest <发布摘要>
```

升级取得原 run 排他锁，归档旧源码指针、配置、输入和 checkpoint 边界，再更新发布版本。主身份、研究目录、对话、记忆、skills、计划和作业状态原地保留。交接正文以独立反馈记录登记，目标与协议保持原文，更新的通用指引进入新的 input revision。事务中断后使用同一发布摘要继续处理；普通恢复会先检查未完成事务。

发布、输入热加载及人类反馈的完整操作约定见[操作者监督](operator-supervision.md)。

## 夜间窗口与防休眠

```bash
bash scripts/start-rsi.sh --night-window --resume-from "artifacts/rsi/<锚点run>"
```

窗口为北京时间 21:00 到次日 09:00。到点保存本地恢复状态并结束 Agent，远程 Controller 继续管理已提交作业。下一个窗口按发布记录更新或恢复原研究。`A3_NIGHT_WINDOW_RUN_ID` 可指定对应锚点，要求受管理的 run 通过此入口恢复。

macOS 脚本自动使用 `caffeinate -is`，运行期间防止空闲休眠，退出后释放。保持供电、开盖和网络连接。需要关闭终端后继续运行时，可显式后台启动：

```bash
mkdir -p artifacts/rsi
nohup bash scripts/start-rsi.sh --night-window --resume-from "artifacts/rsi/<锚点run>" \
  > artifacts/rsi/launcher.log 2>&1 &
```

主机重启后重新运行夜间入口。不要并发启动同一 run，也不要并发复用同一个启动日志。

## 常用工具和作业流程

`job_submit` 接受命令参数数组、源码路径和资产引用。工具自动计算本地源码哈希；训练数据与权重使用只读 exports。独立且输入就绪的任务可先并行提交，再用 `wait` 等待终态或操作者通知。Controller 负责排队、GPU 分配与生命周期。

`job_status` 和 `job_list` 查询实际状态，`job_artifact` 按字符分页读取结果并返回文件哈希。`search_files` 查找源码与记录，`edit` 做精确局部修改。预检、技能与联网工具同样提供具体参数定义，详细契约见[执行接口](../agent-inputs/shared/execution.md)。

采集与评估通过[episode 执行器](episode-executor.md)管理完成台账和并发。作业脚本从环境读取 `A3_OUTPUTS`、`A3_EPISODE_EXECUTOR` 及资产路径，避免沿用另一后端的路径。

计划由 `plan` 保存；长期经验由 `memory` 保存；`recall` 从原始历史检索观测。部署的研究 skill 与 Agent 自己保存的 skill 均通过 `skills` 发现和读取。工作上下文达到压缩门槛后必须生成完整摘要，32k 等目标预算覆盖整个下一轮输入；机制见[上下文与记忆](context-reliability.md)。

## 独立的新研究任务

需要另一个目标或独立实验时，使用 `--harness-commit` 新建 run，也可通过 `--run-harness <已发布保留run或包目录>` 明确选择来源。每个新任务都必须使用 `--agent-inputs-dir <任务目录>` 提供带版本的 goal.md、acceptance.md 和 protocol.md。新任务建立自己的活动计划和对话，保留来源与获授权历史。日常 Harness 维护使用原地发布流程。

## 查找状态与产物

- `artifacts/rsi/<run-id>/`：输入版本、Host 事件、模型用量与会话登记。
- `workspace/<run-id>/<session-id>/`：研究代码、checkpoint、记忆、skills、计划和原始轨迹。
- `artifacts/rsi/<run-id>/.host/release-upgrades/`：原地升级事务和旧状态边界。
- 执行端配置的运行根目录：持久 job 元数据、日志、输出与只读 exports。

排查时先读取 `loop-failure.json`、`pending-calls.json`、Gateway 事件和对应 job 的实际状态。提交结果未知时沿返回的 job ID 查询；源码或依赖错误由 Agent 修复，Controller、认证和部署问题交给维护者。目录职责见[项目结构](project-structure.md)。
