# A3 Research Agent

面向 A3 MuJoCo 的自主机器人 Policy 研究 Agent。

夜间由单个 Agent 自主研究 Policy、采集、训练、评估与分析结果。白天由人类和 Codex 依据原始执行轨迹维护 Harness，包括模型调用、工具、上下文、记忆机制、调度与恢复。Agent 负责报告过程问题及证据，并继续修改研究代码和管理研究知识。

模型接入复用 mini-SWE-agent 的多提供商支持。当前执行方案是在本地运行 Agent，通过 SSH 把仿真、渲染和计算任务交给远程执行端（Docker 或受信任的 Linux 进程后端）；A3 仿真项目在独立的 `a3_dual_arm_sim` 仓库中维护。

## 安装与准备

需要 Python 3.11+、uv、可用的模型服务、本地 Docker，以及配置好的 A3 执行端。远程受信任进程后端的环境、数据盘与资源调度配置见[进程执行](docs/process-backend.md)。在仓库根目录安装：

```bash
git submodule update --init --recursive
uv sync --python 3.11
cp .env.example .env
```

实现与部署资源按组件统一维护于 `src/a3_research_agent/`。安装包、源码目录与完整载荷的说明见[目录职责](docs/project-structure.md)。

按[环境配置](docs/setup.md)和[使用教程](docs/tutorial.md)填写 `.env`、准备本地与远程镜像。模型认证由 Host 保管；个人部署参数与运行材料不进入 Git。

```bash
bash scripts/build-bootstrap-image.sh
bash scripts/start-rsi.sh --check
```

远程已完成/失败/取消的 job 容器在日志和终态可靠归档后自动回收，工作区、输出和导出快照保留；归档失败或容器状态不确定则保留，后续状态查询会重试。此策略不清理其他人的容器、不删除镜像，也不新增定时任务；详见[执行接口](agent-inputs/shared/execution.md#远程资产输出与额度)。

## 三种运行方式

macOS 上启动脚本会自动使用 `caffeinate -is` 包裹 Agent runtime，在运行期间防止空闲休眠，退出后释放。下列三种启动方式均包含这项保护。长程运行请接通电源、保持开盖，并避免关机或重启；屏幕可以自动熄灭。保持运行、后台执行与检查方法见[长程运行说明](docs/tutorial.md#长程运行与防休眠)。

### 初始运行：从 SWE Agent 开始

```bash
bash scripts/start-rsi.sh
```

启动通用 mini-SWE-agent，在隔离工作区完成一次性引导。常规实验使用维护者选定的保留 Harness。

首个研究进程建立后，由同一个 Agent 推进实验。独立作业并行提交，采集与评估统一使用 episode 执行器。

文档按读者职责分工，见 [职责表与输入装载链](docs/agent-inputs.md#文档职责与读者)。用户和父助手的低频监督、每次热加载或中断前的明确批准及操作核对统一见 [操作者监督](docs/operator-supervision.md)。这些文档不加入 Agent 输入。

部署方给 Agent 的输入统一维护于 [agent-inputs/](agent-inputs/README.md)。第一阶段保留专项 [instructions](agent-inputs/phase-one/instructions.md)；第二阶段直接组合 SOUL 与 [目标与验收](agent-inputs/phase-two/)及 [protocol](agent-inputs/shared/protocol.md)，无独立 instructions 文件。Host 每次只向主 Agent 和接任候选提供所选阶段的 Prompt；普通 `/sessions` worker 被禁止。第二阶段从保留版本接续的输入选择见[使用教程](docs/tutorial.md#选择阶段)，研究范围见[第二阶段目标](agent-inputs/phase-two/goal.md)。

运行目录使用 Host 本地时间命名，例如 `artifacts/rsi/run-20261031-110415-123456/`，可直接看出启动日期和时间；末段为微秒。

把起点记作 $H_0$，第一轮最终保留的 Harness 记作 $H_1$：

$$
H_0 \xrightarrow{\text{cold start}} H_1.
$$

### 中断恢复：继续原目标

```bash
bash scripts/start-rsi.sh --resume-from "artifacts/rsi/<run-id>"
```

`--resume-from` 也可由操作者明确恢复因有效外部 blocked 而主动停止的当前主 Agent：保持同 run、身份、工作区、模型与完整上下文，归档旧终态后继续；不恢复已完成 run、已交权旧主或已关闭会话。不是自动重试；失败留下 pending 时先核查，不直接重启。详见[恢复协议](agent-inputs/shared/execution.md#上下文-checkpoint-与-resume)。

将 `<run-id>` 替换为要恢复的运行标识。恢复保留模型对话、工具结果和生成的 Harness，通过原 checkpoint 继续；目标、验收和部署方 Prompt 加载启动时的当前版本。缺少恢复入口、会话已主动提交或存在结果不明的工具调用时会明确报错。

恢复沿用原 run 目录、时间戳、主身份、工作区与资源权限，轨迹继续追加。中断会保留可恢复状态；详细行为见[恢复说明](docs/tutorial.md#中断恢复保留原任务继续推进)。修改 `agent-inputs/` 后开展新任务，应使用下面的版本迭代入口。

### 版本迭代：明确选择父版本

```bash
bash scripts/start-rsi.sh --run-harness "artifacts/rsi/<run-id>"
```

将 `<run-id>` 替换为选定运行的实际标识。启动器读取该运行明确保留的结果，加载代码并自动提供授权历史，版本化当前 `agent-inputs/` 作为新目标。被选择的父版本成为本轮起点：

$$
H_1 \xrightarrow{\text{next run}} H_2 \xrightarrow{\text{next run}} H_3.
$$

必须显式指定来源目录，不需要人工整理接续包。下一轮从哪个保留结果开始由调用者明确选择；`latest-run.txt` 只记录最近启动的运行，不参与版本选择。Agent 在结束前声明保留哪些文件和启动命令；Host 保存快照，后续按这份结果加载，具体协议见[教程](docs/tutorial.md#让本轮结果能够直接用于下一轮)。

这条命令由操作者明确选择新任务的起点。常规昼夜接续见[操作者监督](docs/operator-supervision.md#夜间运行窗口)。

只给第一阶段保留 Harness 补充联网工具 E 时，使用版本迭代的专用 profile：

```bash
bash scripts/start-rsi.sh --phase phase-one --task-profile web-extension \
  --run-harness "artifacts/rsi/<第一阶段保留run-id>"
```

新 run 验证维护者已接入的联网工具。继承既有代码和知识，并使用新上下文。

### 第一阶段 F：Skills 工具补充（需单独批准启动）

已准备 [F 目标](agent-inputs/phase-one-skills/goal.md)、[验收](agent-inputs/phase-one-skills/acceptance.md)与[执行协议](agent-inputs/phase-one-skills/instructions.md)。使用 `--phase phase-one --task-profile skills-extension --run-harness <已完成 E 的 run>` 从 ready E 保留包新建 F，验证现有技能工具与持久复用，不重验 A–E、不启动第二阶段。可追加本轮明确批准的 `--initial-feedback-file`。入口准备不代表 F 已运行或工具已实现，详见[教程](docs/tutorial.md#只补-fskills-工具新-run)。

## 项目文档

完整入口见 [docs/README.md](docs/README.md)。

- [使用教程与完整启动参数](docs/tutorial.md)：准备、初始运行、中断恢复、版本迭代、轨迹查看和故障定位。
- [系统架构](docs/architecture.md)：两阶段 RSI、运行单元、数据流、版本与身份、执行边界。
- [环境配置](docs/setup.md)：依赖、模型适配器、认证与 A3 执行环境。
- [目录职责](docs/project-structure.md)：源码、候选、报告和运行产物分别存在哪里。
- [开发路线与实现状态](docs/roadmap.md)
- [第三方依赖说明](third_party/README.md)
- [Agent 输入版本与热加载](docs/agent-inputs.md)：版本维护、启动记录与确认更新。

### 自定义 retained Harness 新任务

已有保留包可通过 `--run-harness artifacts/rsi/<来源-run-id> --agent-inputs-dir <完整输入目录>` 启动独立新任务。输入由操作者完整指定，不受 E/F 专项 profile 限制；新任务清空旧任务上下文与完成状态，保留代码、skills 和长期记忆。`--resume-from` 则继续原任务。文件清单、快照与验收格式见 [Agent 输入说明](docs/agent-inputs.md#通用新任务交接包--完整输入目录)。
