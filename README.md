# A3 Research Agent

面向 A3 MuJoCo 的 auto research Agent。从通用 mini-SWE-agent 出发，让 Agent 自己形成调用 A3、运行实验、比较 Policy 和改进自身 Harness 的工作方式。

**Policy** 决定机械臂怎样运动；**Harness** 组织模型调用、工具执行、实验记录、记忆和后继运行。人类提前准备目标、验收与受控执行环境，SWE Agent 自己编写并迭代专项实现。阶段一验证 Policy 优化、Harness 轨迹反思、实验隔离与持久记忆；第二阶段面向持续的 Policy 与 Harness 联合优化。

模型接入复用 mini-SWE-agent 的多提供商支持。当前执行方案是在本地运行 Agent，通过 SSH 把仿真、渲染和计算任务交给远程 Docker；A3 仿真项目在独立的 `a3_dual_arm_sim` 仓库中维护。

## 安装与准备

需要 Python 3.11+、uv、可用的模型服务、Docker，以及配置好的 A3 执行端。在仓库根目录安装：

```bash
git submodule update --init --recursive
uv sync --python 3.11
cp .env.example .env
```

按[环境配置](docs/setup.md)和[使用教程](docs/tutorial.md)填写 `.env`、准备本地与远程镜像。模型认证由 Host 保管；个人部署参数与运行材料不进入 Git。

```bash
bash scripts/build-bootstrap-image.sh
bash scripts/start-rsi.sh --check
```

## 三种运行方式

macOS 上启动脚本会自动使用 `caffeinate -is` 包裹 Agent runtime，在运行期间防止空闲休眠，退出后释放。下列三种启动方式均包含这项保护。长程运行请接通电源、保持开盖，并避免关机或重启；屏幕可以自动熄灭。保持运行、后台执行与检查方法见[长程运行说明](docs/tutorial.md#长程运行与防休眠)。

### 初始运行：从 SWE Agent 开始

```bash
bash scripts/start-rsi.sh
```

启动通用 mini-SWE-agent，在独立 Docker 工作区中依据 `evals/goal.md` 与 `evals/acceptance.md` 构建并演化 auto research Harness。运行无需逐轮人工确认；Agent 可以自行创建候选、测试和继续迭代。

运行目录使用 Host 本地时间命名，例如 `artifacts/rsi/run-20261031-110415-123456/`，可直接看出启动日期和时间；末段为微秒。

把起点记作 $H_0$，第一轮最终保留的 Harness 记作 $H_1$：

$$
H_0 \xrightarrow{\text{cold start}} H_1.
$$

### 中断恢复：继续原目标

```bash
bash scripts/start-rsi.sh --resume-from "artifacts/rsi/<run-id>"
```

将 `<run-id>` 替换为要恢复的运行标识，用于网络中断、进程退出等情况。恢复保留原运行的冻结目标、候选代码和历史证据，由 Agent 找回进展继续推进。已进入 Harness 运行模式的任务会加载其保存的 Harness 版本；初始构建过程则通过 mini 接续。

新运行会分配新的 Docker 会话与认证，旧进程内存和模型上下文不会原样恢复。修改 `evals/` 后希望开展新一轮任务，应使用下面的版本迭代入口。

### 版本迭代：明确选择父版本

```bash
bash scripts/start-rsi.sh --run-harness "artifacts/rsi/<run-id>"
```

将 `<run-id>` 替换为选定运行的实际标识。启动器读取该运行明确保留的结果，加载代码并自动提供授权历史，冻结当前 `evals/` 作为新目标。被选择的父版本成为本轮起点：

$$
H_1 \xrightarrow{\text{next run}} H_2 \xrightarrow{\text{next run}} H_3.
$$

必须显式指定来源目录，不需要人工整理接续包。多个分支都可以保留可用结果，下一轮从哪个分支开始由调用者明确选择；`latest-run.txt` 只记录最近启动的运行，不参与版本选择。Agent 在结束前声明保留哪些文件和启动命令；Host 保存快照，后续按这份结果加载，具体协议见[教程](docs/tutorial.md#让本轮结果能够直接用于下一轮)。

这条命令用于人在运行之间主动发起下一轮。单次无人交互运行中，Agent 也可以通过 `/sessions` 创建并执行后继 Harness；两种入口都使用同一套候选版本、Docker 执行、权限与资源管理。Agent 可以自行推进多代，用户无需为每一代重新执行命令。

## 项目文档

完整入口见 [docs/README.md](docs/README.md)。

- [使用教程与完整启动参数](docs/tutorial.md)：准备、初始运行、中断恢复、版本迭代、轨迹查看和故障定位。
- [系统架构](docs/architecture.md)：两阶段 RSI、运行单元、数据流、版本与身份、执行边界。
- [环境配置](docs/setup.md)：依赖、模型适配器、认证与 A3 执行环境。
- [目录职责](docs/project-structure.md)：源码、候选、报告和运行产物分别存在哪里。
- [开发路线与实现状态](docs/roadmap.md)
- [第三方依赖说明](third_party/README.md)
