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

## 长程运行与防休眠

在 macOS 上，`scripts/start-rsi.sh` 自动通过系统自带的 `caffeinate -is` 执行 Agent runtime，覆盖初始运行、恢复与版本迭代，无需安装其他工具。`-i` 防止空闲休眠，`-s` 在接通电源时请求阻止系统休眠；保护随 runtime 退出而释放。脚本允许屏幕自动熄灭，不修改系统的长期电源设置。其他系统继续使用普通执行路径；当前未实现 Windows 防休眠适配。

连续多天运行时，请接通稳定电源、保持笔记本开盖、保留 Docker 与网络连接，并将系统更新重启安排在运行结束之后。`caffeinate` 无法保证断电、强制关机、重启或合盖后的程序继续执行。出现中断时使用 `--resume-from` 找回已保存进展。

前台运行时保持终端开启。希望关闭终端后继续运行，可以显式放入后台：

```bash
mkdir -p artifacts/rsi
nohup bash scripts/start-rsi.sh --run-harness "artifacts/rsi/<run-id>" \
  > artifacts/rsi/launcher.log 2>&1 &
```

先将 `<run-id>` 替换为明确选定的前驱，再执行；初始运行可去掉 `--run-harness` 参数，恢复则换为 `--resume-from`。`nohup` 处理终端断开，脚本内的 `caffeinate` 处理 macOS 休眠。`launcher.log` 是启动与运行输出，原始证据仍保存在各自 run 目录；不要并发复用同一个启动日志文件。

macOS 可在另一个终端运行 `pmset -g assertions`，检查活动的 `caffeinate` 与防休眠记录。命令和参数的系统说明见本机 `man caffeinate`；Apple 的[电源管理接口说明](https://github.com/apple-oss-distributions/IOKitUser/blob/main/pwr_mgt.subproj/IOPMLib.h)说明了休眠断言的适用范围。

## 初始运行：从通用 SWE Agent 构建 Harness

先阅读或调整 `evals/goal.md` 和 `evals/acceptance.md`。当前阶段一包含四项：一次 Policy 优化尝试、一次轨迹反思驱动的 Harness 优化、受限执行与跨实验隔离、持久记忆的跨会话检索与实际复用。专项实现由 SWE Agent 决定。

```bash
bash scripts/start-rsi.sh
```

脚本调用 `runtime/local/launch.py`，创建独立工作区，冻结目标与验收，启动初始 mini。模型可以通过 bash 在构建 Docker 中读写文件、运行程序；生成后的 auto research Agent 使用自己实现的受限工具，并由固定部署边界控制实际权限。

启动器将选定源码复制到独立工作区，包含当前未提交的源码改动，并排除认证、忽略的运行产物与报告工程。它在隔离副本中建立与这些实际文件一致的 Git 基线，让 Agent 从干净的工作区开始；主项目的提交和未提交状态保持原样。

初始通用 Agent 记作 $H_0$，本轮最终保留的版本记作 $H_1$：

$$
H_0 \xrightarrow{\text{experiments and feedback}} H_1.
$$

保留版本可以包含新工具、实验流程、记忆和交接方式。阶段一允许拒绝没有收益的候选，不要求每次修改都提高分数或降低 token。

启动后终端会显示 `Run directory: ...`。该目录保存在 `artifacts/rsi/`，同时更新 `artifacts/rsi/latest-run.txt`。正式运行不设总 step、代数、费用或总时长上限；单条命令、单个作业及并发量仍有部署限制。

run ID 使用 Host 系统本地时间，格式为 `run-YYYYMMDD-HHMMSS-ffffff`，最后六位是微秒。例如 `run-20261031-110415-123456` 表示 2026 年 10 月 31 日 11:04:15.123456 启动。创建目录时若仍重名，会追加 `-2`、`-3` 等序号，分别保存并发运行。

初始 SWE 只启动一次，自主组织实现、迭代与验收；需要交接时，由 SWE 自己创建后继并传递状态。Host 保留正在运行的后继和 Gateway，不定时重建 Agent，不因未完成而重派任务。全部 worker 停止后记录结果；未完成如实记录，不自动重新开始。结果字段见 [Host 执行接口](../runtime/local/README.md#整轮结束与未完成交接)。

## 中断恢复：保留原任务继续推进

网络变化或进程异常退出后，先恢复模型连接、Docker 和 SSH，再运行：

```bash
bash scripts/start-rsi.sh --resume-from "artifacts/rsi/<run-id>"
```

将 `<run-id>` 替换为实际标识。如果确认最近启动的运行就是恢复对象，也可以显式读取最近运行指针：

```bash
bash scripts/start-rsi.sh --resume-from "$(cat artifacts/rsi/latest-run.txt)"
```

恢复入口沿用旧运行的目标、验收、执行说明和 bootstrap 配置。初始 mini 的恢复加载原 `trajectory.json` 中完整模型消息、工具结果与调用/费用统计，继续下一次模型调用，不重新加入任务或历史索引提示词；保留不透明的 provider 上下文，不把它转成可见摘要。原始轨迹不覆盖。

已主动提交的会话不能用 resume 重新打开；下一代应使用明确保留版本。发现工具请求已发出而结果尚未保存时，恢复会拒绝自动执行，须先查明原请求是否成功，避免重复提交作业。模型配置须与原运行一致，认证由 Host 提供。

SWE 生成的 Harness 自主管理其 checkpoint。它须在主 session 工作区保存 `resume.json`，格式与 `continuation.json` 相同，但 command 必须显式加载 checkpoint，files 包含执行源码、模型消息及必要运行状态。Host 按声明启动；没有恢复入口就报错，不用新上下文启动来冒充恢复。Host 不解析或代写专项记忆与恢复策略。

恢复会建立新 Docker 和授权身份；当前实现不恢复旧 Gateway 的会话注册表、已清理的容器和旧资源权限。模型消息可完整保留，运行环境的资源连接需要 Harness 的恢复入口处理；依赖旧资源身份的任务不能宣称完整环境恢复。不要在原运行仍正常推进时重复启动恢复。

## 版本迭代：从已保留的 Harness 开始新任务

上一轮完成并保留 $H_1$ 后，可以修改当前 `evals/`，再启动下一轮：

```bash
bash scripts/start-rsi.sh --run-harness "artifacts/rsi/<run-id>"
```

必须给出选定的运行目录，将 `<run-id>` 替换为实际标识。启动器读取该目录的明确保留结果，创建独立工作区和新身份，自动把授权历史提供给本轮主会话，并使用**当前仓库**的目标与验收。

没有参数的 `--run-harness` 会直接报错。`latest-run.txt` 只记录最近启动的运行，用于查找日志；它不代表最佳版本、验收通过版本或搜索中的父节点。多个分支都有可用结果时，调用者应明确选择本轮的父分支。

$$
H_1 \xrightarrow[\text{current goals}]{\text{new run}} H_2.
$$

从上一轮的 $H_1$ 开始，并不会重新执行 $H_0$ 的通用构建流程。历史材料通过只读索引按需访问；检索、摘要、经验选择和记忆模块由 Agent 自己实现。

已有运行只完成 A/B/C、当前目标新增 D 时，使用这条版本迭代路径：保留旧 Harness 与授权历史，采用当前 A/B/C/D 目标。Agent 自行核对已有证据、补充必要验证，并实现和验收持久记忆。具体职责见[历史归档与 Agent 记忆](architecture.md#历史归档与-agent-记忆的职责)。

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

没有有效声明时，结果会标记为缺失，下一轮不会用文件时间猜测优胜候选，也不会自动回到通用 Agent。跨轮启动只读取明确发布的 `retained.json` 与对应版本快照。

### 多个前驱怎样选择

如果已经有分支 $H_1^{(a)}$ 与 $H_1^{(b)}$，两者都可继续演化，应先选择本轮的前驱 $H_p$，再执行：

$$
H_{\mathrm{new}} = E(H_p; G_{\mathrm{current}}).
$$

这里 $E$ 表示一次演化过程，$G_{\mathrm{current}}$ 是本轮目标。时间上最后启动的运行不决定 $H_p$。外部命令中的 run 目录明确定位父运行，其 `retained.json` 再定位固定的 `version_id` 与 commit。

目前每轮发布一个对外保留结果。同轮内部还有其他可用候选时，由 Harness 明确发布需要继续的分支或可启动 manifest；外部启动器不会扫描候选并代选优胜者。自动提供历史是文件传递机制，父版本选择是实验决策。

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
| `--run-harness <目录>` | 必须显式指定 run 或源码 manifest 目录；加载该结果并冻结当前目标 |
| `--check` | 执行部署预检后退出，不启动 Agent；可与 `--run-harness` 一起校验所选起点 |
| `--verify-deployment` | 运行有界的真实 Agent 部署检查：最多 24 次模型调用、600 秒 |
| `-h`、`--help` | 显示启动器帮助 |

`--run-harness` 不能和 `--resume-from` 或 `--verify-deployment` 同时使用。`--check` 会在启动阶段预检后直接退出；`--verify-deployment` 的结果放在 `artifacts/validation/deployment/`，不改变正式 RSI 的最近运行指针，也不代表 A/B/C/D 正式通过。

```bash
bash scripts/start-rsi.sh --help
bash scripts/start-rsi.sh --check --run-harness "artifacts/rsi/<run-id>"
bash scripts/start-rsi.sh --verify-deployment
```

可通过 `A3_RSI_ENV_FILE` 指定另一份部署配置。正式运行默认无限步数，资源和单作业时限由部署层维护，不由上述参数扩大。

## 看运行轨迹与定位错误

初始 mini 的轨迹位于 `<run目录>/trajectory.json`，可以使用其浏览器：

```bash
.venv/bin/mini-extra inspect "$(cat artifacts/rsi/latest-run.txt)/trajectory.json"
```

方向键切换与滚动，`$` 跳到最后，`R` 重新加载，`q` 退出浏览器。退出浏览器不影响 Agent。

`--run-harness` 直接启动指定的保留 Harness，不再额外启动通用 mini。首次冷启动的轨迹为顶层 `trajectory.json`。已生成 Agent 的轨迹格式和名称由其实现决定，通常位于 `candidates/<session-id>/workspace/`，主 session 标识在 `startup.json` 中。

`supervision.json` 保存当前监督阶段、构建回合与后继状态，`supervisor-events.jsonl` 保存阶段变化。`outcome.json` 记录 Host 接受的整轮终态；实际能力仍需回到验收与原始证据复核。

`model-usage.jsonl` 保存 Gateway 的模型调用用量；`gateway-events.jsonl` 保存请求开始、结束、耗时、状态码和错误层；`session-events.jsonl` 保存父子关系。请求响应的 `X-Request-ID` 可用于关联一次故障。只有请求开始、尚无结束时，先结合模型请求、session 和远程 job 状态判断，不凭短暂日志静止判定卡死。

本地候选名额不足会返回 `candidate_capacity` 和重试间隔；远程 `resource_busy` 表示当前资源不可用。Agent 应查询和等待。终止整次运行会回收本次登记的会话、远程作业与隔离 fixture；未完成清理保存在 `cleanup-errors.json`。运行目录和原始证据继续保留。

## 版本快照与独立工作区

**commit** 是选定源码的固定快照，由 Host 在本轮私有 Git 仓库中保存。它不会给本项目的开发分支提交，也不会自动 push。**worktree** 是从快照展开的独立工作目录，供一个执行身份读写；创建新 worktree 会继承原代码，同时保留旧目录供复核。

Docker 只挂载自己的源码目录，Git 对象库和其他 worktree 留在 Host。跨轮关系通过 `parent_run`、`source_version` 和保留结果记录，代码版本、会话身份与远程作业标识分别追踪。这个版本层提供复跑与隔离；Agent 如何分工和避免重复实验，仍由后续协调策略决定。
