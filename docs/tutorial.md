# A3 Research Agent 使用教程

本教程从第一次启动开始，说明怎样让 SWE Agent 构建 Harness、怎样恢复中断的任务，以及怎样让上一轮的保留版本继续演化。所有命令在本仓库根目录执行。运行系统各部分如何配合，见[系统架构](architecture.md)。

## 先理解一次运行

一次 **run** 是启动脚本到退出之间的一段完整运行，拥有自己的目标副本、工作区、轨迹和结果。一次 run 内可以有多个 Agent 会话、多个 Harness 版本和多个 A3 实验。

一次 **session** 是在独立 Docker 中执行一个 Harness 或检查程序。它有自己的身份、工作区和日志，可以通过受控接口请求模型或计算资源。一次 **job** 是交给执行端的仿真、渲染或计算任务；Agent 提交后查询状态，再取回产物。

例如，Agent 可以先运行基线 Policy，再提出一个修改并提交三个种子的对照实验。它读到结果后决定拒绝候选，随后回看自己的工具调用轨迹，修改 Harness，再创建一个 session 实际测试修改。整个过程可以在同一个 run 内完成。

## 第一次启动前准备什么

本地安装 Python 3.11+、uv 和可用的 Docker。当前本地部署入口面向 macOS Docker Desktop；执行端使用 Docker Engine，GPU 作业还需 NVIDIA Container Toolkit。依赖、模型认证和 A3 环境的细节见[环境配置](setup.md)，执行端镜像与 Controller 见[远程部署说明](remote-execution.md)。

```bash
git submodule update --init --recursive
uv sync --python 3.11
cp .env.example .env
```

在 `.env` 中填写自己的配置：

| 配置 | 用途 |
| --- | --- |
| `MODEL_NAME`、`MODEL_CLASS` | 模型标识与 mini 的模型适配器；认证按模型服务要求保存在 Host |
| `A3_WORKER_MODEL_NAME`、`A3_SUMMARY_MODEL_NAME` | 可选的固定辅助／摘要模型；主身份和候选行为验证仍使用主模型，配置见[上下文与模型路由](context-reliability.md) |
| `A3_MODEL_CONTEXT_TOKENS` | 启用经核实窗口的token预算；辅助模型必须同时配置各自窗口，不能把文件字节数当token数 |
| `MODEL_COST_TRACKING` | 成本统计方式；没有真实价格时费用保持未知 |
| `A3_PROJECT_ROOT` | 本地 A3 Git 工作区，供启动器制作隔离副本 |
| `A3_LOCAL_IMAGE` | 执行初始构建与候选 Harness 的本地镜像 |
| `A3_REMOTE_HOST` | 已配置免交互连接的 SSH 目标 |
| `A3_REMOTE_CONTROLLER` | 执行端本仓库的 `src/a3_research_agent/execution/controller/__main__.py` 路径 |
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

## 选择阶段

目标与验收分别位于 `agent-inputs/phase-one/`、`agent-inputs/phase-two/`；第一阶段专项指令位于 `agent-inputs/phase-one/instructions.md`；第二阶段无单独 instructions，直接组合 SOUL、goal、acceptance、shared/protocol（仍映射为 /run/input/protocol.md）。Host 每次选择一个阶段，向主 Agent 和接任候选提供对应阶段的完整输入，不叠加另一个阶段或 common Prompt。

新运行默认第一阶段，也可显式写 `--phase phase-one`。第二阶段从明确选定的保留 Harness 开始，输入选择命令为：

```bash
bash scripts/start-rsi.sh --phase phase-two --run-harness "artifacts/rsi/<run-id>"
```

此命令使用阶段一交付的 Harness 代码与持久记忆，初始化新的模型上下文，原保留 checkpoint 不变。当前支持 checkpoint.json 为 JSON 消息列表的入口；其他格式会明确报错。正式第二阶段实验还需完成[方案中的训练环境、预算与成果接续检查](plans/2026-10-31-phase-two.md)，输入选择通过不代表这些准备已完成。

`--resume-from` 自动沿用原 run 的阶段，加载该阶段的当前版本文件；无需重复 `--phase`。显式指定不同阶段会报错。跨阶段使用新 run 和 `--run-harness`，保留来源关联。原始历史输入与轨迹保持原样。

## 初始运行：从通用 SWE Agent 构建 Harness

先阅读或调整 `agent-inputs/phase-one/goal.md` 和 `agent-inputs/phase-one/acceptance.md`。当前阶段一包含四项：一次 Policy 优化尝试、一次轨迹反思驱动的 Harness 优化、受限执行与跨实验隔离、持久记忆的跨会话检索与实际复用。专项实现由 SWE Agent 决定。

```bash
bash scripts/start-rsi.sh
```

脚本调用 `python -m a3_research_agent.cli.launch`，创建独立工作区，版本化目标与验收，启动初始 mini。模型可以通过 bash 在构建 Docker 中读写文件、运行程序；生成后的 auto research Agent 使用自己实现的受限工具，并由固定部署边界控制实际权限。

启动器将选定源码复制到独立工作区，包含当前未提交的源码改动，并排除认证、忽略的运行产物与报告工程。它在隔离副本中建立与这些实际文件一致的 Git 基线，让 Agent 从干净的工作区开始；主项目的提交和未提交状态保持原样。

初始通用 Agent 记作 $H_0$，本轮最终保留的版本记作 $H_1$：

$$
H_0 \xrightarrow{\text{experiments and feedback}} H_1.
$$

保留版本可以包含新工具、实验流程、记忆和交接方式。阶段一允许拒绝没有收益的候选，不要求每次修改都提高分数或降低 token。

启动后终端会显示 `Run directory: ...`。该目录保存在 `artifacts/rsi/`，同时更新 `artifacts/rsi/latest-run.txt`。正式运行不设总 step、代数、费用或总时长上限；单条命令、单个作业及并发量仍有部署限制。

run ID 使用 Host 系统本地时间，格式为 `run-YYYYMMDD-HHMMSS-ffffff`，最后六位是微秒。例如 `run-20261031-110415-123456` 表示 2026 年 10 月 31 日 11:04:15.123456 启动。创建目录时若仍重名，会追加 `-2`、`-3` 等序号，分别保存并发运行。

初始 SWE 只启动一次，自主组织实现、迭代与验收；需要交接时，由 SWE 自己创建后继并传递状态。Host 保留正在运行的后继和 Gateway，不定时重建 Agent，不因未完成而重派任务。全部研究进程停止后记录结果；未完成如实记录，不自动重新开始。结果字段见 [Host 执行接口](../agent-inputs/shared/execution.md#整轮结束与未完成交接)。

## 中断恢复：保留原任务继续推进

网络变化或进程异常退出后，先恢复模型连接、Docker 和 SSH，再运行：

```bash
bash scripts/start-rsi.sh --resume-from "artifacts/rsi/<run-id>"
```

将 `<run-id>` 替换为实际标识。如果确认最近启动的运行就是恢复对象，也可以显式读取最近运行指针：

```bash
bash scripts/start-rsi.sh --resume-from "$(cat artifacts/rsi/latest-run.txt)"
```

人工回复一个已断开的自停 run 时，恢复命令应明确携带通知：

```bash
bash scripts/start-rsi.sh --resume-from "artifacts/rsi/<run-id>" --feedback-file "<已批准的恢复通知.md>"
```

通知说明修复了什么、基础设施检查不替代哪些 Agent 验收，以及继续工作的边界。`registered` 不等于已送达；检查 HumanFeedback 的 `forwarded` 事件及 model-usage 的 `human_feedback_ids` 才能确认成功转发。通知经现有独立 user feedback 进入完整历史，不另建上下文；同一反馈不会在历史中重复追加。若上次 blocked 恢复后又因旧上下文自停，只有已完成 attempt 的原 blocked 归档与当前主身份关联核验通过，并显式提供通知，才恢复该窄情况；不是放开普通 incomplete 或已完成任务。

同一个 `--resume-from` 也支持：当前主 Agent 因有效外部 blocked 主动 self-stop 后，由操作者明确恢复。Host 必须已停止，Docker 停止已确认，主身份未完成、未 closed、未交权，checkpoint 和外部阻塞证据有效。保留同 run、身份、工作区、模型和全部历史；旧 terminal／console／outcome 先归档，再把活动 outcome 设为 continue，并仅轮换相同 scope 的临时 token。不会自动重试、重注入反馈或复活其他已停止会话。若出现 `.host/blocked-resume/<attempt>/attempt.json` pending，CLI 拒绝重复启动，先核查实际 container 和归档，不直接删除 pending 或改停止标志。详见[恢复协议](../agent-inputs/shared/execution.md#上下文-checkpoint-与-resume)。

恢复直接使用指定的原 run 目录，保持 run ID、时间戳、当前主 Agent 身份、全部工作区文件与版本库。目标、验收、执行说明和部署方 Prompt 加载启动时仓库当前文件，记录版本号与摘要。startup.json 不改写，恢复时间和输入清单另记在 resume-events.jsonl。初始 mini 保留原 `trajectory.json` 中的用户/模型对话、工具结果与调用/费用统计，刷新部署方 system 消息后继续，不重新加入整套任务；不透明 provider 上下文按原样保留。

已主动提交的会话不能用 resume 重新打开；下一代应使用明确保留版本。发现工具请求已发出而结果尚未保存时，恢复会拒绝自动执行，须先查明原请求是否成功，避免重复提交作业。模型配置须与原运行一致，认证由 Host 提供。

SWE 生成的 Harness 自主管理其 checkpoint。它须在主 session 工作区保存 `resume.json`，格式与 `continuation.json` 相同，但 command 必须显式加载 checkpoint，files 包含执行源码、模型消息及必要运行状态。Host 按声明启动；没有恢复入口就报错，不用新上下文启动来冒充恢复。Host 不解析或代写专项记忆与恢复策略。

主会话使用 Git worktree 时，恢复入口只接受指向该 session 自身 worktree 的 Host 工作区链接；其他链接仍被拒绝。

Host 将身份令牌、父子关系、主身份、trial、作业归属和 fixture 记录原子保存到本轮 `.host/state.json`，仅 Host 可读，不挂载进 Agent Docker。恢复使用原 Gateway 端口和授权 scope；暂停中的候选 Docker 直接接回，不重放模型或工具调用。主进程确已退出时，才在同一工作区以同一身份执行其 `resume.json`，其他已完成会话 不重新运行。

Ctrl-C 中断时暂停候选 Docker，并保留远程作业及其权限；远程作业继续受原时限与资源分配约束。整轮正常结束时仍清理资源。已取消或失败的作业保持真实状态，恢复不会把它变成成功或自动重跑。进程锁阻止两个 Host 同时启动同一个 run；端口被占用或恢复入口缺失时明确报错。

旧版运行没有私有 Host 快照时，只允许从已停止运行的 Host 会话元数据导入原关系与 scope，并轮换丢失的本轮令牌；模型消息、源码和工作区不改写。无法核实原身份或模型时拒绝恢复，不创建新 run。

## 版本迭代：从已保留的 Harness 开始新任务

上一轮完成并保留 $H_1$ 后，可以修改当前 `agent-inputs/`，再启动下一轮：

```bash
bash scripts/start-rsi.sh --run-harness "artifacts/rsi/<run-id>"
```

必须给出选定的运行目录，将 `<run-id>` 替换为实际标识。启动器读取该目录的明确保留结果，创建独立工作区和新身份，自动把授权历史提供给本轮主会话，并使用**当前仓库**的目标与验收。

没有参数的 `--run-harness` 会直接报错。`latest-run.txt` 只记录最近启动的运行，用于查找日志；它不代表最佳版本、验收通过版本或搜索中的父节点。有多个历史保留结果时，调用者明确选择本轮来源；本轮只沿单一版本链推进。

$$
H_1 \xrightarrow[\text{current goals}]{\text{new run}} H_2.
$$

从上一轮的 $H_1$ 开始，并不会重新执行 $H_0$ 的通用构建流程。历史材料通过只读索引按需访问；检索、摘要、经验选择和记忆模块由 Agent 自己实现。

已有运行只完成 A/B/C、当前目标新增 D 时，使用这条版本迭代路径：保留旧 Harness 与授权历史，采用当前 A/B/C/D 目标。Agent 自行核对已有证据、补充必要验证，并实现和验收持久记忆。具体职责见[历史归档与 Agent 记忆](architecture.md#历史归档与-agent-记忆的职责)。

`--run-harness` 也接受带 `run.json` 的源码目录，例如归档快照。这适用于首次导入一个可运行版本；自动跨轮接续通常直接使用 run 目录。导入源码目录时不会凭空附带某次历史运行。

### 只补 E：联网工具新 run

在已批准的任务范围内，明确选择第一阶段保留 run：

```bash
bash scripts/start-rsi.sh --phase phase-one --task-profile web-extension \
  --run-harness "artifacts/rsi/<第一阶段保留run-id>"
```

这是新 run，不是 resume 旧 self-stop 会话，也不重新冷启动 mini。代码、持久记忆和原恢复／身份能力沿用；新工作副本初始化新模型上下文与 checkpoint，源快照不改。旧记录经只读索引按需检索，不预加载全量历史。A–D 已记录结论带来源继承，标为 historical-not-recertified，不重跑、不重新认证；原 Host 归档缺口和 retained readiness 不等于本轮独立 A–D 审计。

新副本中已知旧任务状态先归档到 `inherited-task-state/` 并记录摘要：task.txt 使用 E 目标，trajectory 从空文件继续追加，旧 progress 完成状态转为继承记录，旧 run-outcome 改为 continue。仅处理新副本，源快照和长期记忆不改；普通同 run resume 保留本轮状态；显式外部 blocked 恢复只归档并解除旧 terminal 声明，不做新任务初始化。Harness 自己更新 resume／continuation 的保存逻辑，确保新工具模块、记忆及必要源码／来源引用不会被旧固定清单遗漏或覆盖。

只加载 [E 目标](../agent-inputs/phase-one-web/goal.md)、[E 验收](../agent-inputs/phase-one-web/acceptance.md)和[专用协议](../agent-inputs/phase-one-web/instructions.md)。保留 Harness 自己实现工具并让实际模型成功 search、fetch 公开正文，完成来源引用和离线错误 fixture；不训练、下载模型／PDF、运行 A3 或 GPU 实验。只做必要局部检查，不强制完整 H0/H1 或 A–D 循环。完成 E、结果包与 continuation 后退出；配置、key、额度或网络阻断时保存 blocked 后停止，不能把离线测试当真实通过。

此 profile 仅适用于第一阶段 retained 来源，不接受冷启动或第二阶段。普通第一阶段仍按原 A–D 目标运行。恢复本轮中断用 `--resume-from <本轮目录>`，自动保持 web-extension profile 与本轮上下文；显式冲突 profile 被拒绝。E 完成的新保留结果可作为 `--phase phase-two --run-harness <E轮目录>` 的来源，但 E 完成不认证前轮整个历史。配置沿用项目根目录私有 `.env`，无需分享 key。启动、发布反馈、热加载或中断须遵守[操作者授权规则](operator-supervision.md)。

如用户已批准本轮初始反馈，可在上述命令追加 `--initial-feedback-file <已批准的UTF-8文件>`。文件须非空且不超过 64 KiB；它仅适用于新建第一阶段 web-extension retained run，不能与 resume 或旧 `--feedback-file` 混用。新 checkpoint 包含 E 目标与初始反馈两个 user messages，新副本保存 `initial-feedback.md` 及 task_context 中的摘要、字节数和来源审计。未提供时只有目标消息；没有 profile 固定反馈，不自动注入私有内容。这是新任务输入，不冒充运行中的 Host 动态反馈事件；原热加载／反馈审批规则不变。

### 只补 F：Skills 工具（新 run）

先取得本轮 F 的明确启动授权，再从完成 E 且 ready 的保留来源启动：

```bash
bash scripts/start-rsi.sh --phase phase-one --task-profile skills-extension \
  --run-harness artifacts/rsi/<已完成E的run目录> \
  --initial-feedback-file <本轮已批准的UTF-8反馈文件>
```

初始反馈可省略；它说明选定来源、当前 F 目标、人工准备及范围，不代表验收通过。提供时须非空 UTF-8、最多 64 KiB，加入 fresh checkpoint 的目标消息之后。旧 E 反馈只作历史，不自动注入新任务。不要用 `--resume-from` 给完成的 E 更换任务，也不要恢复旧第二阶段。

F 只加载 [goal](../agent-inputs/phase-one-skills/goal.md)、[acceptance](../agent-inputs/phase-one-skills/acceptance.md)、[instructions](../agent-inputs/phase-one-skills/instructions.md)与共享 SOUL/执行输入。Agent 自己实现技能发现、按需读取、实际执行和复用、证据状态更新、持久清单与重载；Host 不代写工具。A–E 按历史继承，不重跑、不重新认证。仅轻量隔离验证，不训练、下载或占用 GPU，不追求性能收益。

F 新副本保持源码和持久记忆，用新上下文与 F 任务状态；旧任务材料归档保留，不覆盖 E 源。中断后的同轮恢复保持 skills-extension 和完整上下文，任何人工干预按操作者规则通过 human feedback 说明。F 完成后保存可复用 retained 并退出，不自动开始第二阶段。目录和 profile 已准备，不等于 F 已执行或 skills 已实现。

## 让本轮结果能够直接用于下一轮

Agent 在结束前，通过已有文件写入能力留下 `continuation.json`：

```json
{
  "command": ["python", "next_agent.py"],
  "files": ["next_agent.py", "memory/lessons.json"]
}
```

`command` 是下一轮的启动命令数组，`files` 是完整的 UTF-8 启动文件清单。保留原版时也可以声明原版；选择哪个版本由 Agent 决定。初始构建 Agent 把声明放在自己的 `/workspace/run/`，已启动的 Harness 把声明放在主会话 `/workspace/`。路径相对声明所在工作区，不能指向其他身份、凭据目录或 Host 文件。

人类无需逐轮整理这些文件。Host 在全部研究进程停止后读取声明，将选定文件保存成固定 Git 快照，并在 run 根目录写 `retained.json`。下一次 `--run-harness` 直接从快照读取；之后改动旧工作区也不会改变已保留版本。本地保存和从保留版本启动不设 128 个文件、20 MiB 的上传上限，实际仍需足够的磁盘空间与内存；路径、身份和凭据保护继续生效。Agent 经 HTTP 创建候选或提交版本时，每次上传仍限 128 个 UTF-8 文件、合计 20 MiB。二进制数据保存在授权历史中，由 Harness 按需读取或恢复，记忆系统可以使用数据库或其他格式。

没有有效声明时，结果会标记为缺失，下一轮不会用文件时间猜测优胜候选，也不会自动回到通用 Agent。跨轮启动只读取明确发布的 `retained.json` 与对应版本快照。

### 多个前驱怎样选择

若有多个历史保留版本，先明确选择本轮的前驱 $H_p$，再按单一版本链继续：

$$
H_{\mathrm{new}} = E(H_p; G_{\mathrm{current}}).
$$

这里 $E$ 表示一次演化过程，$G_{\mathrm{current}}$ 是本轮目标。时间上最后启动的运行不决定 $H_p$。外部命令中的 run 目录明确定位父运行，其 `retained.json` 再定位固定的 `version_id` 与 commit。

目前每轮发布一个对外保留结果。候选按线性版本逐次试运行，不并行推进研究分支；外部启动器不会扫描候选并代选优胜者。自动提供历史是文件传递机制，父版本选择是实验决策。

### Agent 可以在同一轮内继续迭代

`--run-harness` 是运行之间的外部启动入口。正在执行的 Agent 也可以通过自己的工具请求 `POST /evolution/trials`，加载候选版本并继续执行：

$$
H_k \xrightarrow{\text{evaluate and select}} H_{k+1}.
$$

外部启动与内部后继都调用同一个 Host 候选执行服务，使用相同的版本保存、Docker、身份、资源与回收规则。目标、版本选择和上下文交接由 Harness 组织，因此第二阶段可以在一次长运行内自主推进很多代。

## 参数表

`scripts/start-rsi.sh` 将参数传给 `a3_research_agent.cli.launch`：

| 参数 | 行为 |
| --- | --- |
| 无参数 | 从通用 mini 开始新的冷启动，版本化当前目标 |
| `--resume-from <run目录>` | 保留会话身份、代码和对话；加载启动时当前目标、验收与 Prompt 后继续 |
| `--run-harness <目录>` | 必须显式指定 run 或源码 manifest 目录；加载该结果并版本化当前目标 |
| `--phase {phase-one,phase-two}` | 新运行默认第一阶段；恢复自动沿用原阶段；第二阶段需明确保留来源 |
| `--task-profile {standard,web-extension,skills-extension}` | 默认 standard；web-extension 从第一阶段 retained 新建 E；skills-extension 从已完成 E 的 ready retained 新建 F；恢复沿用原 profile |
| `--initial-feedback-file <文件>` | 可选已批准初始反馈，仅新建第一阶段 web-extension / skills-extension retained run；非空 UTF-8，最多 64 KiB |
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

本地候选名额不足会返回 `candidate_capacity` 和重试间隔；远程 `resource_busy` 表示当前资源不可用。Agent 应查询和等待。整轮正常完成会回收登记的会话、远程作业与隔离 fixture；中断启动器会暂停候选、保留状态和资源以便恢复。未完成清理保存在 `cleanup-errors.json`。运行目录和原始证据继续保留。

已退出的研究进程 无需等整轮结束才清理。Host 每两秒检查，并在确认退出、保存控制台与退出记录后自动删除其 container。工作区里的 checkpoint、记忆、源码和产物保留，仍可用原 session ID 查询状态与结果。运行和暂停中的研究进程、其他会话 及远程作业均不受这项回收影响。单个会话 清理失败时，`candidates/<session_id>/session.json` 与状态接口提供 `cleanup_error`，Host 存活期间自动重试。运行目录和镜像、构建缓存仍需单独管理磁盘空间。

`--resume-from` 可以恢复已经自动删除 container 的主 Agent：Host 根据原工作区的 `resume.json` 重新创建 Docker，沿用原 run、身份、上下文和记忆；已完成的辅助会话 不重跑。恢复能力依赖 Harness 退出前保存有效 checkpoint，通过自身停止接口明确结束的会话不自动重启；操作者明确恢复有效外部 blocked 当前主身份时，适用上文的窄例外。

Agent 写下完成或阻塞说明后，其运行程序还需要实际退出。生成的 Harness 应在自身循环中实现正常退出分支；Host 也提供 `POST /sessions/self/stop`，允许候选保存状态后只结束自己，后继和远程作业继续保留。该终结操作可能在 HTTP 响应返回前结束进程，不能把保存放在调用之后。Host 不根据验收声明自动杀进程，也不自动重启通过此接口结束的 session。接口详见 [Host 执行说明](../agent-inputs/shared/execution.md#独立候选执行)。

## 版本快照与独立工作区

**commit** 是选定源码的固定快照，由 Host 在本轮私有 Git 仓库中保存。它不会给本项目的开发分支提交，也不会自动 push。**worktree** 是从快照展开的独立工作目录，供一个执行身份读写；创建新 worktree 会继承原代码，同时保留旧目录供复核。

Docker 只挂载自己的源码目录，Git 对象库和其他 worktree 留在 Host。跨轮关系通过 `parent_run`、`source_version` 和保留结果记录，代码版本、会话身份与远程作业标识分别追踪。这个版本层提供复跑与隔离；当前 Agent 通过记忆和真实作业状态避免重复实验。

## 本轮研究内容的读取

同一 run 的已登记研究身份使用自己的令牌，通过会话与作业接口读取本轮源码、记忆、可见轨迹、日志、产物、作业状态及已授权只读 exports。按问题取必要片段并保留版本和来源，不必为了让候选读前任资料建立额外 worker。可写工作区仍独立；取消、封存和管理权限沿本身份或已采用 lineage 检查。其他 run、Host 凭据和外部用户数据不因本轮共享而开放。

analyzer 读取本轮汇总统计并返回建议，全部本轮身份均可读取；仅当前主 Agent 发起分析并记录采纳、拒绝或推迟。接口与限额见[轨迹分析](trajectory-analysis.md)。

## Harness 试运行与主身份交接

独立且输入就绪的远程作业先在授权资源内并行提交，再统一查询与收集；有依赖的作业按依赖推进。采集与评估中的独立 episode 统一通过 `/run/a3/episode_executor.py` 执行，固定科学参数、seed、快照和输出，复用已完成结果。

普通脚本、参数与结果解析问题由当前 Agent 在当前任务中修复并验证；需要 Host 权限、凭据、服务或资源配置变更时，保留错误证据，向操作者报告所缺条件和具体操作。

每代主 Agent 只核查自己负责的实验闭环：先记录假设、实际执行，再依据结果决定保留、拒绝或证据不足，满足本次交接条件才交权。交接已确认结论、适用条件、版本、证据引用及明确未完成任务；后继可将前任已确认结论作为已知结论，无需读全历史消息或逐条重验。遇到明确矛盾可针对性核查，不能默认全历史审计；未完成或未知不能当作通过。接手未完事项是新的执行责任，不接管前任过去的合规责任。候选先完成本次改动与接任能力验证，接任后执行自身研究，不要求在接任前完成整个 run。最终主身份汇总可引用各代结论，不重新执行或审计前任实验；各阶段科学门槛不变，不因交接而重复最终网格或已完成的配对比较。

当前研究 Agent 自己实现 Policy 与 Harness、采集数据、训练评估、修复脚本并判断验收。局部检查使用本身份工具与远程作业，普通 `POST /sessions` worker 创建被拒绝。完整 Harness 使用唯一 `/evolution/trials` 候选：前任等待候选自测，候选交权前只自测并等待；拒绝回收或由原候选接任，旧主交权后退出。单次运行不分叉并行研究 Agent。

常规演化中，主 Agent 先提出 Harness 改动和验收清单，启动一个完整候选进行实际执行；检查结果后决定保留或拒绝。拒绝回到当前版本，保留则让同一候选进程成为下一代主 Agent，原主 Agent 保存交接后退出。无需人工提供下一轮任务。前任在 trial 期间等待自测结果，候选交权前不开展独立研究；Policy 优化在这些迭代之间进行。E-only profile 只做实现与实际加载所需的局部验证；需要主身份更替时遵守此协议，不无条件追加完整演化或 Policy 实验。

`evolution.json` 记录主身份、版本化清单、候选版本和选择理由。原始轨迹与各 session 代码仍保留在 run 中。主身份交接不等同于故障 resume：新一代的消息和记忆如何承接由 SWE 实现；网络故障恢复则要求显式 checkpoint。详见 [执行协议](../agent-inputs/shared/execution.md#主身份与候选试运行)。

## 调整核心提示词

`agent-inputs/shared/bootstrap.yaml` 只维护初始 mini 的运行参数、Bash/提交约定和路径。主 Agent 和接任候选共同加载 SOUL、所选阶段的 goal 与 acceptance；第一阶段另加载对应任务的 instructions，第二阶段另加载 `agent-inputs/shared/protocol.md`。调整要求时修改对应输入文件并递增版本，再启动或显式热加载。Host 为这两类身份提供同一份阶段输入；普通 worker 创建被拒绝。

编辑源文件不会自动影响已经运行的实验；恢复启动会采用当前版本。每次热加载或中断前须用户明确批准；具体审批、预览、发布与生效核对见 [外界监督与运行干预](operator-supervision.md#热加载版本化输入)。版本规则、职责表和装载链见 [Agent 输入管理](agent-inputs.md)。

## 同一起点开展多次独立实验

反复使用同一个明确来源的 `--phase phase-two --run-harness <run目录>`，每次生成独立 run 和工作副本。Host 读取原保留 commit，不读取后来被修改的活动 worktree；第二阶段在副本中初始化新上下文，代码与持久记忆保持原样。每轮更新自己的 checkpoint，源快照不会变长。

需要继续同一轮中断任务时使用 `--resume-from <该轮目录>`，保留该轮完整上下文。希望从某个阶段二成果再次开始新任务时，明确选择那个成果所在 run，仍走 --run-harness。源历史可只读查阅，不必全部装入新模型上下文。

远程并发、超时、存储额度与只读资产目录通过 `.env.example` 中的 A3_REMOTE_* 选项配置；详见[远程存储与执行限制](remote-execution.md)。这些部署参数由 Host 管理，不向 Agent 开放修改权限。

## 多个小作业共享 GPU

部署方设置 A3_REMOTE_SHARE_GPUS=1，使用 A3_REMOTE_MAX_GPUS 控制同时使用的物理卡数；A3_REMOTE_MAX_JOBS=0 关闭按作业数量的固定限制。同一卡有余量时可继续接收小作业，运行位置由 Host 选择。

A3_REMOTE_GPU_CAPACITY_MB 与 A3_REMOTE_GPU_HEADROOM_MB 分别设置每卡可预留总额和缓冲，缓冲默认 2048 MiB（2 GiB）。共享模式允许与外部进程同卡，只按实际显存与预留量判断剩余空间；总使用卡数仍受 A3_REMOTE_MAX_GPUS 限制。Agent 提交作业时可用 gpu_memory_mb 记录预计峰值，省略时采用 A3_REMOTE_GPU_MEMORY_MB。详细算法与范围见[远程调度说明](remote-execution.md#显存共享调度)。


## 第二阶段的大文件交接

阶段二沿用所选保留 Harness，并开始新的模型上下文。预训练 backbone、tokenizer 和相关资源由人类在执行端准备；模型目录和离线缓存只读提供，作业不下载缺失依赖。下载的 ACT backbone 是否构成完整可运行 Policy，应按资产清单判断。

训练产生的 checkpoint、数据和视频由 Agent 自己选择保留。固定 Host 提供 exports：先封存稳定文件，验证 ready 后明确授权给后继，后继作业只读加载。接口、文件路径和清理规则见 [Host 大文件交接说明](../agent-inputs/shared/execution.md#大文件只读交接)。这不需要人类组织交接包。

跨轮复用时，Agent 将引用写入 continuation.json 的 remote_exports；运行结束后的 retained.json 同时保存代码版本和大文件引用。用户仍使用 `--phase phase-two --run-harness <明确选定的run目录>` 启动。源数据与授权需要保留在同一执行端资源根目录，不能删掉仍被后续运行引用的快照。模型、数据训练及六格真实成绩由正式 auto research 产生，环境检查不计为正式结果。

## 恢复时加入人类反馈

恢复反馈的命令、输入限制、历史定位与留档说明统一见 [操作者文档](operator-supervision.md#恢复时加入人类反馈)。恢复或中断的实际操作须在用户批准范围内；反馈不能代替真实验收证据。

## 运行中热加载人类反馈

每次热加载或中断前须用户明确批准。反馈预览、确认、投递核对与身份变化处理统一见 [操作者文档](operator-supervision.md#运行中热加载人类反馈)。研究 Agent 自主核查、采纳或不采纳反馈，不承担外界审批职责。

## 第二阶段按需网页研究

可选 Tavily key 只在本机 Host `.env` 配置，具体位置、账户用量与无 per-run 累计上限语义见[环境配置](setup.md#可选研究网页网关tavily)。默认关闭。网关只提供受控 HTTP 能力：H1 自己实现 function 工具适配与注册，并通过必要的局部测试和实际加载验证；需要主身份更替时遵守现有接任流程。可先用上面的第一阶段 web-extension profile 补 E，再将新保留版本用于第二阶段；旧 Harness 不会自动出现新工具，无需重跑 A–D。

先核查记忆与已有资料，只针对模型/配置选择、训练调参、网络结构、兼容性或异常闭环中的具体缺口检索；资料转成预登记假设，再用本地可比实验检验。不是每个实验都必须联网；各代责任、科学指标和阈值不变。网页内容不可信，私有日志与源码不能发给搜索服务，模型与依赖仍由人类离线准备。

文档与启动入口本身不代表已完成真实联网验证，也不自动热加载或中断现有 run。启动须明确选定保留来源及已批准的 profile／阶段；低频观察与反馈不能替代 Agent 自主研究，每次热加载、反馈发布或运行中断仍遵守操作者批准范围。
