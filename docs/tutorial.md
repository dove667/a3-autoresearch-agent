# A3 Research Agent 使用教程

本教程从第一次启动开始，说明怎样让 SWE Agent 构建 Harness、怎样恢复中断的任务，以及怎样让上一轮的保留版本继续演化。所有命令在本仓库根目录执行。运行系统各部分如何配合，见[系统架构](architecture.md)。

## 先理解一次运行

一次 **run** 是启动脚本到退出之间的一段完整运行，拥有自己的目标副本、工作区、轨迹和结果。一次 run 内可以有多个 Agent 会话、多个 Harness 版本和多个 A3 实验。

一次 **session** 是在独立 Docker 中执行一个 Harness 或检查程序。它有自己的身份、工作区和日志，可以通过受控接口请求模型或计算资源。一次 **job** 是交给执行端的仿真、渲染或计算任务；Agent 提交后查询状态，再取回产物。

例如，Agent 可以先运行基线 Policy，再提出一个修改并提交三个种子的对照实验。它读到结果后决定拒绝候选，随后回看自己的工具调用轨迹，修改 Harness，再创建一个 session 实际测试修改。整个过程可以在同一个 run 内完成。

## 第一次启动前准备什么

本地安装 Python 3.11+、uv 和可用的 Docker。当前本地部署入口面向 macOS Docker Desktop；执行端使用 Docker Engine，GPU 作业还需 NVIDIA Container Toolkit。依赖、模型认证和 A3 环境的细节见[环境配置](setup.md)，执行端镜像与 Controller 见[远程部署说明](../runtime/remote/README.md)。

```bash
git submodule update --init --recursive
uv sync --python 3.11
cp .env.example .env
```

在 `.env` 中填写自己的配置：

| 配置 | 用途 |
| --- | --- |
| `MODEL_NAME`、`MODEL_CLASS` | 模型标识与 mini 的模型适配器；认证按模型服务要求保存在 Host |
| `MODEL_COST_TRACKING` | 成本统计方式；没有真实价格时费用保持未知 |
| `A3_PROJECT_ROOT` | 本地 A3 Git 工作区，供启动器制作隔离副本 |
| `A3_LOCAL_IMAGE` | 执行初始构建与候选 Harness 的本地镜像 |
| `A3_REMOTE_HOST` | 已配置免交互连接的 SSH 目标 |
| `A3_REMOTE_CONTROLLER` | 执行端本仓库的 `runtime/remote/remote_jobs.py` 路径 |
| `A3_REMOTE_RUN_ROOT` | 执行端作业与资源锁的共同根目录 |
| `A3_REMOTE_IMAGE` | 已包含 A3 与计算依赖的作业镜像 |

同一执行端的所有 Controller 必须共用资源锁根目录。不要为每个候选复制一套 GPU 锁。`.env` 不进入 Git，镜像名称和路径均由部署者设置。

构建本地镜像并检查执行端：

```bash
bash scripts/build-bootstrap-image.sh
bash scripts/start-rsi.sh --check
```

`--check` 核对本地镜像、模型适配器配置与远程 Controller 健康响应，不执行模型请求或 GPU 实验。真实认证、仿真、渲染和训练组件应各自验证。

如果只想先手动体验原始 mini，可运行 `uv run mini`。它是第三方 mini 的交互入口；下面的 RSI 脚本使用无人交互启动配置和受控 Docker 执行环境。

## 初始运行：从通用 SWE Agent 构建 Harness

先阅读或调整 `evals/goal.md` 和 `evals/acceptance.md`。当前阶段一包含四项：一次 Policy 优化尝试、一次轨迹反思驱动的 Harness 优化、受限执行与跨实验隔离、持久记忆的跨会话检索与实际复用。专项实现由 SWE Agent 决定。

```bash
bash scripts/start-rsi.sh
```

脚本调用 `runtime/local/launch.py`，创建独立工作区，冻结目标与验收，启动初始 mini。模型可以通过 bash 在构建 Docker 中读写文件、运行程序；生成后的 auto research Agent 使用自己实现的受限工具，并由固定部署边界控制实际权限。

初始通用 Agent 记作 $H_0$，本轮最终保留的版本记作 $H_1$：

$$
H_0 \xrightarrow{\text{experiments and feedback}} H_1.
$$

保留版本可以包含新工具、实验流程、记忆和交接方式。阶段一允许拒绝没有收益的候选，不要求每次修改都提高分数或降低 token。

启动后终端会显示 `Run directory: ...`。该目录保存在 `artifacts/rsi/`，同时更新 `artifacts/rsi/latest-run.txt`。正式运行不设总 step、代数、费用或总时长上限；单条命令、单个作业及并发量仍有部署限制。

## 中断恢复：保留原任务继续推进

网络变化或进程异常退出后，先恢复模型连接、Docker 和 SSH，再运行：

```bash
bash scripts/start-rsi.sh --resume-from "$(cat artifacts/rsi/latest-run.txt)"
```

也可以明确指定运行目录：

```bash
bash scripts/start-rsi.sh --resume-from artifacts/rsi/<run-id>
```

恢复入口核对上一轮冻结目标和验收的摘要，并沿用这两份文件，即使仓库的 `evals/` 已经更新。它导出旧代码、实验报告、反思和可见轨迹，创建接续运行，并记录 `parent_run`。

如果中断发生在通用 mini 的构建阶段，由 mini 读取恢复索引接着构建。如果中断的是已经启动的 Harness，则加载其明确保留的版本；尚无最终声明时，加载该轮保存的执行起点，并提供中间产物，让 Agent 判断从哪里继续。

这里恢复的是原任务与磁盘进展。新 Docker 会话、新认证和新的模型调用上下文由启动器创建；旧进程、失败中的 API 请求和旧令牌不会恢复。Agent 仍需根据记录判断哪些作业结果有效，哪些操作需要重试。不要在原运行仍正常推进时重复启动恢复。

## 版本迭代：从已保留的 Harness 开始新任务

上一轮完成并保留 $H_1$ 后，可以修改当前 `evals/`，再启动下一轮：

```bash
bash scripts/start-rsi.sh --run-harness
```

无参数时读取 `artifacts/rsi/latest-run.txt`。也可以显式选择最近运行，或指定某一轮：

```bash
bash scripts/start-rsi.sh --run-harness latest
bash scripts/start-rsi.sh --run-harness artifacts/rsi/<run-id>
```

第一条从最近运行读取，第二条从指定运行读取。启动器加载所选运行的保留快照，创建独立工作区和新身份，自动把授权历史提供给本轮主会话，并使用**当前仓库**的目标与验收。

$$
H_1 \xrightarrow[\text{current goals}]{\text{new run}} H_2.
$$

从上一轮的 $H_1$ 开始，并不会重新执行 $H_0$ 的通用构建流程。历史材料通过只读索引按需访问；检索、摘要、经验选择和记忆模块由 Agent 自己实现。

`--run-harness` 也接受带 `run.json` 的源码目录，例如归档快照。这适用于首次导入一个可运行版本；自动跨轮接续通常直接使用 run 目录。导入源码目录时不会凭空附带某次历史运行。

### 让本轮结果能够直接用于下一轮

Agent 在结束前，通过已有文件写入能力留下 `continuation.json`：

```json
{
  "command": ["python", "next_agent.py"],
  "files": ["next_agent.py", "memory/lessons.json"]
}
```

`command` 是下一轮的启动命令数组，`files` 是完整的 UTF-8 启动文件清单。保留原版时也可以声明原版；选择哪个版本由 Agent 决定。初始构建 Agent 把声明放在自己的 `/workspace/run/`，已启动的 Harness 把声明放在主会话 `/workspace/`。路径相对声明所在工作区，不能指向其他身份、凭据目录或 Host 文件。

人类无需逐轮整理这些文件。Host 在退出前读取声明，将选定文件保存成固定 Git 快照，并在 run 根目录写 `retained.json`。下一次 `--run-harness` 直接从快照读取；之后改动旧工作区也不会改变已保留版本。当前源码传递上限为 128 个 UTF-8 文件、合计 20 MiB；大型原始日志和二进制数据保存在授权历史中，由 Harness 按需读取或恢复，记忆系统可以使用数据库或其他格式。

没有有效声明时，结果会标记为缺失，下一轮不会用文件时间猜测优胜候选，也不会自动回到通用 Agent。已有早期冷启动的 `workspace/run/final/entry.py` 约定由兼容入口自动识别；该兼容方式不需要人工接续包。

### Agent 可以在同一轮内继续迭代

`--run-harness` 是运行之间的外部启动入口。正在执行的 Agent 也可以通过自己的工具请求 `POST /sessions`，加载候选版本并继续执行：

$$
H_k \xrightarrow{\text{evaluate and select}} H_{k+1}.
$$

外部启动与内部后继都调用同一个 Host 候选执行服务，使用相同的版本保存、Docker、身份、资源与回收规则。目标、分支选择和上下文交接由 Harness 组织，因此第二阶段可以在一次长运行内自主推进很多代。

## 参数表

`scripts/start-rsi.sh` 将参数传给 `runtime/local/launch.py`：

| 参数 | 行为 |
| --- | --- |
| 无参数 | 从通用 mini 开始新的冷启动，冻结当前目标 |
| `--resume-from <run目录>` | 沿用该轮冻结目标，恢复代码与证据后继续 |
| `--run-harness [latest或目录]` | 从保留结果或源码 manifest 启动新一轮，冻结当前目标；省略值时为 `latest` |
| `--check` | 执行部署预检后退出，不启动 Agent；可与 `--run-harness` 一起校验所选起点 |
| `--verify-deployment` | 运行有界的真实 Agent 部署检查：最多 24 次模型调用、600 秒 |
| `-h`、`--help` | 显示启动器帮助 |

`--run-harness` 不能和 `--resume-from` 或 `--verify-deployment` 同时使用。`--check` 会在启动阶段预检后直接退出；`--verify-deployment` 的结果放在 `artifacts/validation/deployment/`，不改变正式 RSI 的最近运行指针，也不代表 A/B/C/D 正式通过。

```bash
bash scripts/start-rsi.sh --help
bash scripts/start-rsi.sh --check --run-harness
bash scripts/start-rsi.sh --verify-deployment
```

可通过 `A3_RSI_ENV_FILE` 指定另一份部署配置。正式运行默认无限步数，资源和单作业时限由部署层维护，不由上述参数扩大。

## 看运行轨迹与定位错误

初始 mini 的轨迹位于 `<run目录>/trajectory.json`，可以使用其浏览器：

```bash
.venv/bin/mini-extra inspect "$(cat artifacts/rsi/latest-run.txt)/trajectory.json"
```

方向键切换与滚动，`$` 跳到最后，`R` 重新加载，`q` 退出浏览器。退出浏览器不影响 Agent。

`--run-harness` 跳过初始 mini，因此没有这份顶层轨迹。已生成 Agent 的轨迹格式和名称由其实现决定；默认保留入口写入 `candidates/<session-id>/workspace/trajectory.jsonl`，主 session 标识在 `startup.json` 中。

`model-usage.jsonl` 保存 Gateway 的模型调用用量；`gateway-events.jsonl` 保存请求开始、结束、耗时、状态码和错误层；`session-events.jsonl` 保存父子关系。请求响应的 `X-Request-ID` 可用于关联一次故障。只有请求开始、尚无结束时，先结合模型请求、session 和远程 job 状态判断，不凭短暂日志静止判定卡死。

本地候选名额不足会返回 `candidate_capacity` 和重试间隔；远程 `resource_busy` 表示当前资源不可用。Agent 应查询和等待。终止整次运行会回收本次登记的会话、远程作业与隔离 fixture；未完成清理保存在 `cleanup-errors.json`。运行目录和原始证据继续保留。

## 版本快照与独立工作区

**commit** 是选定源码的固定快照，由 Host 在本轮私有 Git 仓库中保存。它不会给本项目的开发分支提交，也不会自动 push。**worktree** 是从快照展开的独立工作目录，供一个执行身份读写；创建新 worktree 会继承原代码，同时保留旧目录供复核。

Docker 只挂载自己的源码目录，Git 对象库和其他 worktree 留在 Host。跨轮关系通过 `parent_run`、`source_version` 和保留结果记录，代码版本、会话身份与远程作业标识分别追踪。这个版本层提供复跑与隔离；Agent 如何分工和避免重复实验，仍由后续协调策略决定。
