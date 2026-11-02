# Host提供的执行与验收入口

Version: 0.3.0

部署方运行 gateway、容器引擎、模型认证与远程资源调度。候选 Harness 在独立容器内运行，
通过 HTTP 请求这些固定能力。工具名称、研究逻辑、反思方法和版本选择由 Agent 设计。
目标和验收以本次只读的 `goal.md`、`acceptance.md` 为准。

## 执行主体与证据归属

初始构建者是启动器在隔离构建工作区运行的 mini-SWE-agent。它通过 Bash 编写候选入口，
再用 `POST /sessions` 启动独立进程。该入口就是 Agent 生成的候选 Harness；其中的模型经
自己声明的工具调用 A3、读取结果和选择下一步时，产生的是生成后 Agent 的执行证据。
按实际入口、模型消息、工具调用、权限和结果判断证据归属；候选在报告中自称 builder
不会改变这些调用的来源。验收不要求候选再嵌套生成另一层模型进程。

远程 `/jobs` 是执行仿真、相机和评测代码的受控作业。模型调用位于候选进程，通过 Host
转发；远程作业按部署设计没有模型凭据或网络。候选实际调用受限工具并读取作业结果，
就是这条执行链，不需要在远程作业中再次运行模型。

候选由模型驱动只说明执行主体成立。A/B/C/D 仍分别需要版本化标准要求的真实能力、
版本、比较与选择、权限边界和跨会话记忆复用证据；创建会话、命名工具或保存报告均不能单独验收。

## 请求与权限

gateway 地址和本身份的令牌由 `A3_GATEWAY_URL`、`A3_GATEWAY_TOKEN` 环境变量提供。
请求携带 `Authorization: Bearer <本身份令牌>`，不要把令牌打印到轨迹或存入报告。
模型使用 Responses 接口 `/v1/responses`，别名 `openai/sandbox-model`，适配器 `litellm_response`。
`OPENAI_API_BASE` 与 `OPENAI_API_KEY` 已配置为本轮转发入口；实际模型固定在Host。
调用费用未知时保持未知，token 用量从响应读取。

## 独立候选执行

`POST /sessions` 接受 `{"command": ["python", "entry.py"], "files": {"entry.py": "..."}}`，
返回 `session_id`。上传需要运行的候选文件；128 个 UTF-8 文件合计不超过 20 MiB。
命令参数使用数组。不能指定 Docker 参数、挂载、Host命令、身份或 GPU。

Host创建同级容器，工作目录为 `/workspace`，上传文件写入该目录。
其身份由Host独立分配并注入，调用方拿不到子身份令牌。候选容器不挂载构建者或其他候选的目录、令牌和模型认证。
它只能访问自己的可写工作区、只读 `/run/input` 和只读公共 A3 源码 `/opt/a3`。
无需在容器内创建 namespace、安装 Docker 或运行嵌套容器。
固定限制为 2 CPU、2 GiB 内存、256 个进程、512 MiB 临时目录，同时最多五个活动候选容器。
本地镜像提供 mini 和模型客户端；A3 仿真依赖在远程镜像中。

`GET /sessions/{session_id}` 返回状态、退出码和近期输出，Docker 完成状态为 `exited`。
`GET /sessions/{session_id}/artifacts?path=relative_path` 返回最多 32 MiB 文件的 Base64 内容。
大轨迹使用同一接口的 `offset` 和 `limit` 按字节分段读取，例如
`?path=trajectory.jsonl&offset=0&limit=1048576`。返回 `data`、`offset`、`next_offset`、
`size_bytes` 与 `eof`；按 `next_offset` 继续读取，先拼接解码后的字节，再解析 UTF-8 或 JSONL。
每段最多 32 MiB；只给非零 `offset` 时默认读取 1 MiB。未分段请求超大文件会返回
HTTP 422、`reason: artifact_too_large` 和可重试的 `retry_query`，不改变身份和路径授权。
`size_bytes` 与 `eof` 反映本次读取时观察到的文件末尾；运行中的轨迹可继续追加，之后可从已保存偏移量继续查询。
`GET /sessions` 列出调用方可管理的后代及父关系；上级身份可以查询后代状态、取回产物和管理生命周期。
`GET /sessions?include_status=true` 在同一会话锁下取得列表、状态与退出码，Host 使用此快照判断活动后继，避免并发创建后继时漏记。
同级候选与无关身份不能访问彼此的会话；后代不能管理祖先。
进程正常退出会保留后继候选的执行与身份，结束的进程不再占活动容器名额。

Host 每两秒检查本轮已登记的 worker；状态查询也会尝试回收已退出的 Docker。
确认 Docker 已退出后，先将控制台保存为 `candidates/<session_id>/console.log`，
将退出码、结束状态和保存时间原子落盘到 `session.json`，再删除该 container。
工作区直接保存在 Host 挂载目录，源码、checkpoint、记忆和产物继续保留；
Harness 须在退出前完成自己的写入。Host 不生成缺失的 checkpoint，也不替 Agent 判断验收。
删除后仍能通过原 session ID 查询退出码、近期输出及工作区产物。

container 回收不关闭会话管理关系，不撤销普通退出身份，不取消远程作业、fixture 或后继。
运行和暂停中的 Docker 保留。日志或退出记录保存失败时不删除 container；
清理错误记录在 `session.json` 的 `cleanup_error`，单会话状态查询也返回该字段。
Host 存活期间自动重试，恢复同一 run 后继续处理遗留清理；删除成功后清除此错误。
container 回收不删除镜像、构建缓存、运行目录或历史证据；这些内容仍需单独管理空间。

`GET /sessions/self` 返回调用身份自己的 session 状态，也可用自己的实际 session ID 查询。
初始 mini 使用提交标记退出；没有候选 Docker 的构建者身份不能调用自身 session 接口。

`POST /sessions/self/stop {"reason":"状态已保存"}` 是候选进程的主动结束入口。
Host 仅停止调用者自身的 Docker，确认退出并保存控制台后自动删除 container，
保留工作区，不级联终止后继，不取消远程作业。
这些作业继续受原资源分配和超时约束，并在整轮清理时回收。成功停止后撤销当前会话令牌，
祖先仍可查询该 session 和后继，后继使用自己的身份继续执行。通过此接口主动结束的 session 不由 resume 自动重启。
停止失败时返回可重试错误，身份保持可用。请求不能指定其他身份、挂载或级联操作。
Host 先记录停止意图，再停止进程；若中断留下未确认的停止请求，恢复会明确报错，需先核查实际状态。
日志归档错误记录在 session.json 的 cleanup_error，保留 container 等待重试；
已确认的停止不会因日志错误而变成可恢复进程。

这是终结操作：先保存证据、记忆、checkpoint、run-outcome.json 和 continuation.json，
再单独调用，不与其他写入并行。Docker 先收到正常终止信号，五秒内未退出则强制终止；
调用进程可能在收到 HTTP 响应或保存该工具结果之前结束。Host 的 session-events.jsonl
记录 self_stop_requested 和成功时的 self_stopped；两者分别表示请求与实际停止。
需要完整保存最后一次工具结果时，应让生成的 Harness 自行实现正常 return/break。

`DELETE /sessions/{session_id}` 关闭当前候选；若还有未回收后代，返回 HTTP 409 和后代标识，保持分支不变。
明确放弃整条分支时，使用 `DELETE /sessions/{session_id}?cascade=true`。
分支取消回收该候选及后继候选、相关远程作业和隔离 fixture，保存控制台输出、移除容器并撤销身份，工作区保留。
如果部分资源暂时无法回收，返回错误；父调用方仍可用原 session ID 重试清理。
运行Host始终保留后代管理关系，父进程退出后后代仍可由上级身份管理。
`session-events.jsonl` 保存创建与关闭关系，不保存令牌；候选版本、分数和搜索父节点由研究 Harness 自行记录。
研究 Agent 可以使用自己的固定权限工具和候选代码；工具后端仍经这些部署边界执行。
后继候选可上传修改后的文件再运行；实际使用、反思与比较证据由 Agent 保存。

## 远程 A3 作业

`POST /jobs` 接受 `{"command": [...], "gpu": false, "files": {"probe.py": "..."}}`。
身份由 gateway 注入。需要 GPU 时设置 `gpu: true`，由同一服务器 controller 分配。
作业镜像内 A3 位于 `/opt/a3`，Python、仿真与相机依赖已准备好；命令工作目录为 `/workspace`。
公共参考文件为 `/opt/a3/README.md`、`/opt/a3/docs/workflow_guide.md` 和 `/opt/a3/docs/contracts.md`，示例入口位于 `/opt/a3/examples/`。文档中的 `examples/...` 相对 A3 根目录；在作业工作目录中调用时使用绝对路径，或使用 Python 模块入口。例如检查评测命令：

```json
{"command": ["python", "-m", "a3_dual_arm_sim.workflows.benchmark_cookie_batch_cli", "--help"], "gpu": false, "files": {}}
```

等效示例入口为 `["python", "/opt/a3/examples/benchmark_cookie_batch.py", "--help"]`。不要假设存在 `python -m a3` 或包根目录的 `__main__`；参考文档与这些示例随同一 A3 源码准备进镜像。生成后的 Harness 自行组织文档访问和接口知识交接。
作业根文件系统只读、无网络，只挂载自己的工作区，无 SSH、Docker socket 或 gateway 令牌。
任务结束后通过 `GET /jobs/{job_id}` 查询状态，通过
`GET /jobs/{job_id}/artifacts?path=relative_path` 取回产物；取消为 `DELETE /jobs/{job_id}`。
资源忙时稍后重试。长任务先提交再查询，避免单条本地命令超时。

实际限制以 deployment.json 的 remote_limits 为准。作业可共享同一张 GPU；Host 按实际显存与预留量准入，物理 GPU 数和作业数量分别配置。max_jobs=0 不设作业数量上限，timeout_seconds=0 不设时限。
这是单作业资源边界，总研究 step、候选代数与总时长没有上限。

## 受控双身份验收资源

从需要验收的研究 Agent 身份调用 `POST /fixtures/isolation`，Host创建一个真实的异身份 CPU 作业，
返回 `fixture_id`、`peer_job_id`、`peer_artifact_path` 与哨兵 SHA-256。其身份和认证留在Host。
该作业最多等待 600 秒，并占用一个远程并发名额，不占 GPU。

研究 Agent 使用自己的身份尝试查询、取消、读取这个异身份作业，实际请求应被拒绝。
伪造 `experiment_id` 参数也应被拒绝。用自己的合法作业证明正常访问，
并在真实作业容器中检查无法访问异实验目录、共享基线不可写。
这组资源用于验收，不授权操作其他真实实验。

随后 `GET /fixtures/isolation/{fixture_id}` 由Host核对哨兵未变及异身份作业仍在，返回真实结果。
记录请求、拒绝、核验和正常操作证据后，调用 `DELETE /fixtures/isolation/{fixture_id}` 清理。
每个身份同时最多一个 fixture，可清理后重新创建。启动器退出时也会清理本轮 fixture。
接口成功或Host预检通过不能代替研究 Agent 自己执行 A、B、C 的验收证据。

初始 SWE 只启动一次。它退出后，Host 保持已经启动的后继、Gateway 和资源服务可用，等待所有 worker 停止。Host 不定时重建 Agent，不因未完成或错误重新派发任务，不自动生成审查意见；显式反馈的投递机制见下文。后续进程与交接由 SWE 自己安排。

整轮正常完成后 Host 清理资源；启动器中断会暂停候选并保留资源以便恢复。清理失败保存 `cleanup-errors.json` 供复核。Host 在全部 worker 停止后读取终态清单并进行结构检查；该检查不替代实际验收证据。

### 整轮结束与未完成交接

当前主 Agent 使用 `/workspace/run/run-outcome.json` 声明终态。验收细节与证据格式仍由 Agent 组织；这份清单在所有 worker 停止后用于记录结果：

```json
{
  "status": "complete",
  "acceptance": {
    "A": {"status": "passed", "evidence": ["evidence/policy-report.json"]},
    "B": {"status": "passed", "evidence": ["evidence/harness-report.md"]},
    "C": {"status": "passed", "evidence": ["evidence/isolation-report.json"]},
    "D": {"status": "passed", "evidence": ["evidence/memory-report.md"]}
  }
}
```

验收项取自版本化 `acceptance.md` 的字母标题，所有必需项均为 `passed`、引用文件在本工作区实际存在且非空，并有有效 `continuation.json`，才能声明完成。路径相对 `/workspace/run/`。先将选择的后继证据与入口按授权交接到该目录；Host 不替 Agent 选择候选。Host 检查协议和文件可达性，实际能力仍按版本化标准复核。

未完成时可写 `{"status":"continue","reason":"尚未完成的能力与已有进展"}`，或仅保存进展后提交本回合。提交前须自行安排后继；Host 只等待现有后继，不开启新回合。全部 worker 已停止但未完成时，记录 incomplete。

确需人工处理的外部阻塞使用 `status: blocked` 与 `external_blocker`，字段为 `kind`、`requires_human: true`、`reason`、`required_action`、`evidence`。允许的类别是 `authentication`、`permission`、`unavailable_service` 和 `missing_resource`；同样需要可达证据。候选名额、作业等待、无收益候选和待完成验收都应继续处理。

run-outcome.json 声明结果，Host 等待进程实际停止后再读取它。生成的 Harness 应在自己的循环中实现终态退出，或保存状态后调用自身停止接口；写入终态文件不会触发 Host 自动中断或再次派发任务。

## 候选源码版本

Host 保存候选的 Git commit 和独立 worktree，Docker 只挂载 worktree 中的 `source/`，看不到 `.git` 或其他候选。
上传文件与使用已有版本均由 `/sessions` 创建新身份；同一版本可以运行多次。

- `POST /versions {"files": {"entry.py": "..."}, "parent_version": "可选版本标识"}` 保存明确提交的完整源码快照，返回 `version_id`、`commit` 与父版本。文件清单是完整替换，不是增量补丁；未列出的旧文件不会保留。
- `GET /versions` 只列出本身份获授权的版本。
- `POST /sessions {"command": [...], "version_id": "..."}` 在新 worktree 中运行该版本。不能同时上传文件。
- 原有 `/sessions` 上传文件方式自动保存初始 commit；可增加 `parent_version` 指向有权访问的父版本。后继未指定时，沿用调用身份的执行起点作为父版本。
- `GET /sessions/{session_id}/versions` 由有管理权限的祖先取回并获得后代已保存版本的访问权，用于比较和复跑；同级身份不能调用。

Git 记录进入本轮 `versions/`。工作区内的即时修改不会自动成为 commit，Agent 应将选定源码显式提交到 `/versions`，并把评测与准确的版本标识、协议和结果关联。日志、记忆和任务材料按需显式交接；不要提交认证文件。
候选包的默认文件清单由 `run.json` 指定，Host 不自动扫描历史私有工作区。版本分叉与会话父子关系分别记录，固定并发配额保持不变；跨 Agent 分工、发现与去重尚未提供自动策略。

## 比较条件检查

`POST /checks/comparison` 接受 `baseline` 与 `candidate`，每组声明：

```json
{
  "status": "completed",
  "report_present": true,
  "protocol": {
    "task": "任务标识", "seeds": [1, 2, 3], "workers": 1,
    "step_budget": 500, "randomization": "basic", "success_rule": "原始计分"
  }
}
```

检查任务、种子、worker 上限、步数预算、随机化与成功规则，额外协议字段也参与比较。
返回 `comparable`、`missing`、`differences` 和 `execution_issues`；条件不一致、缺报告或执行失败均不能通过。
这些是对提交字段的机械检查，调用者仍应从实际报告提取并核对协议；服务不会单凭声明证明真实运行可比，也不替 Agent 决定候选优劣。

## 故障与等待记录

每个 Gateway 请求返回 `X-Request-ID`，Host 在 `gateway-events.jsonl` 写开始与结束事件，记录身份、路由模板、状态码、持续时间、错误发生层和异常类别。
模型请求在等待期间也有开始记录；记录不包含请求正文、令牌或异常原文。`attempt: 1` 表示 Gateway 不自动重试，Agent 发起的重试会产生新请求标识。
候选名额不足时，HTTP 409 的 `detail.reason` 为 `candidate_capacity`，并给出最大活动数和建议重试间隔。
每轮最多同时运行 5 个候选或 worker；已退出或已关闭的 session 不占名额。上限在 Host 启动时加载，当前不支持热更新。主身份交接后，旧主 Agent 应退出并释放名额。
远程 `resource_busy` 会记录容量等待；远程调用总耗时含 SSH 与 Controller 处理，当前尚未分拆模型加载、实际计算等内部耗时。

保留的阶段一 `research` 入口仍使用原有 Gateway 路径清单。接入新增 `/versions` 和 `/checks/comparison` 操作由 SWE Agent 在后续候选中自主完成；Host 已准备好相应授权与检查。

## 运行结束与自动接续

主 Harness 在自己的 `/workspace/continuation.json` 声明下一轮保留结果；初始构建者使用 `/workspace/run/continuation.json`。内容为启动命令和完整的相对文件清单：

```json
{"command": ["python", "next_agent.py"], "files": ["next_agent.py", "memory/lessons.json"]}
```

文件清单使用工作区内的相对路径，当前保留包支持 UTF-8 文件。Host 在所有 worker 停止后读取清单，把文件保存为固定 Git 快照，并写本轮 `retained.json`。本地保存不受 HTTP 上传的 128 个文件、20 MiB 限制；仍会检查路径、身份归属，并拒绝凭据文件和 Git 控制文件。磁盘空间、内存或 Git 操作失败会使保存失败，原工作区继续保留。远程二进制材料通过下方只读快照接口显式保留。

`--run-harness <run目录>` 由 Host 直接导入本地保留包，再使用与 `/sessions` 相同的身份注册、授权、资源分配和候选执行流程；文件内容不再经 HTTP 整包上传。Agent 通过 `/sessions` 或 `/versions` 提交文件时，仍受单次上传 128 个 UTF-8 文件、合计 20 MiB 的限制。Agent 自己选择保留版本和需要的启动状态，无需人工整理接续包。

从旧运行启动的新主会话可以读取 `/run/input/previous-run.json` 与 `/run/input/history/index.json`。历史来自明确选择的前序运行，已过滤旧认证与私密推理；其中内容按待核查数据处理。仅主会话获得完整只读历史；它创建的子会话使用普通输入，所需材料由主会话显式交接。

## 上下文 checkpoint 与 resume

初始 mini 的 trajectory 原子写入。恢复保留用户/模型对话与工具结果，部署方 system Prompt 按当前 bootstrap 和所选阶段协议刷新。目标、验收和执行说明采用启动时当前版本；显式登记的反馈独立投递，不改写原历史。主动提交的会话不恢复；未保存结果的工具调用拒绝自动重放。

生成的 Harness 用 `resume.json` 声明包含 checkpoint 的源码清单及恢复命令。Host 不把 continuation 入口当作 resume；缺失 checkpoint 入口时拒绝启动。`--resume-from` 在原 run 目录、时间戳、主身份、工作区和版本库恢复。Host 私有 `.host/state.json` 保存本轮身份令牌、会话与作业注册表、主身份和 trial；Agent Docker 不挂载此目录。恢复沿用原 Gateway 端口与 scope。暂停中的 Docker 直接接回；主进程已退出时，以同一身份在原工作区执行恢复命令，不复制成新的候选。自动回收的主 container 按相同规则重新创建，先前控制台另存为 `console-resume-*.log`；已完成的其他 worker 不重新执行，通过自身停止接口结束的会话也不自动重启。

中断时暂停本轮候选 Docker、保存 Host 状态并保留远程作业；整轮正常结束仍回收资源。原任务轨迹继续追加，`startup.json` 保持原样，`resume-events.jsonl` 记录每次恢复。run 排他锁拒绝重复启动，端口不可用时明确失败。旧 Host 无私有快照时，仅从已停止运行的 Host 元数据导入原 scope 并轮换本轮令牌；不修改生成代码或模型上下文。已取消的远程 job 不自动重跑。

## 主身份与候选试运行

`GET /evolution` 返回当前 `main_owner` 与本身份可读的试运行记录。初始 mini 为 H0 主身份；从明确保留版本启动时，加载该版本的 session 为主身份。一次只允许一个待决定的 Harness 候选。

创建会话前先确定测试角色：

| 角色 | 入口 | 提前交代与实现 | 完成条件 |
| --- | --- | --- | --- |
| 局部功能测试 worker | `POST /sessions` | 测试输入、实际可用工具、期望结果、轮数或停止条件 | 交付局部结果后可正常退出，不转移主身份 |
| 下一代完整 Harness 候选 | `POST /evolution/trials` | 当前完整 Harness 加本次改动，保留执行循环与已有能力，并交接任务状态、改动及接任清单 | 完成试运行，等待主 Agent 决定，保留后以同一进程继续 auto research |

主 Agent 交权前完成自己负责的实验闭环核查，交接已确认结论、适用条件、版本、证据引用与明确未完成任务。后继无需逐条重验前任，未完成或未知不得写成已确认；接手未完事项只承担后续执行责任。候选在接任前无需完成整个 run，先验证本次改动及接任能力，接任后开展自身研究。

主 Agent 先验证局部假设，再将有效改动整合进完整 Harness。测试脚本按其局部任务判断；接任候选须具备完整继续执行能力。启动前核对自己提供的源码、工具、文件与任务能够支持验收。模型调用必须实际带上所需 tools；只发空 tools 的连通请求不能覆盖工具执行。主 Agent 向下一代交接两类测试协议。

当前主 Agent 自行提出改动、固定验收清单并执行以下协议：

1. `POST /evolution/trials`：包含普通 `/sessions` 的 command 与 files 或 version_id，另加 `baseline_version`（已保存且有权使用的 H0 版本）和 `checklist`（改动的验收清单）。Host 将清单、摘要与两版代码标识固定记录，启动候选 H1；主身份仍属于 H0。主 Agent 负责通过显式文件交接让候选读到清单、任务、记忆和必要状态。
2. 候选执行清单并保存结果，等待主身份交接。它可查询 `/evolution` 判断自己是否已成为 main；测试完成后须保持进程可交接。候选不能自行保留或拒绝版本，也不能创建下一代主候选。
3. H0 读取实际结果并自行判断，确认自己负责的实验闭环与接任清单实际满足交接条件。调用 `POST /evolution/trials/{session_id}/decision`，提供 `retain`、`reason`、`evidence`（候选 workspace 内结果路径）。保留时 Host 检查结果路径可读、候选仍在运行，原子转移主身份，不再启动另一份 H1。清单是否达标、是否值得保留由 H0 决定。
4. 拒绝时 Host 回收该候选分支，主身份和 H0 版本不变。保留时 H0 保存交接并退出；H1 的同一进程成为新主 Agent，使用已交接的任务状态继续执行。旧主身份不能再次发起试运行或决定。

候选代码在启动时已保存为不可变 Git commit/worktree。保留决定引用这份实际测试版本；未加载的新修改须作为新的试运行，不把文件的即时编辑当成已测试版本。`evolution.json` 保存主身份变化与逐次决定。Host 不生成改动、记忆或专项验收，也不根据收益自动选择版本。

H0 的已保存执行结果可作为基线证据，无需为了每次改动再启动旧版 Agent；主 Agent 须说明清单与已有能力、过程指标的关系，不能只以候选自称成功决定保留。Policy 实验可由当前主 Agent 穿插执行，不切换主身份。普通 `/sessions` 用于辅助 worker，创建普通 worker 不转移主身份。主身份是唯一的决策权限，试运行期间允许 H0 与候选两个进程共存；H0 交接后应自行退出，Host 不在模型调用中途强制杀掉它。最终主 Agent 核查自身工作，引用各代已确认结论与证据汇总进展，不重新执行或审计前任原实验；未覆盖项保持未完成。它在自己的 workspace 保存最终 run-outcome.json 与 continuation.json；所有 worker 停止后从最终主身份归档。

## 固定核心提示词

`configs/rsi-bootstrap.yaml` 仅维护初始 mini 的参数、Bash/提交约定与启动路径。完整的阶段协议分别维护在 `configs/prompts/phase-one.md` 与 `configs/prompts/phase-two.md`，由本轮 phase 选择。目标和验收来自对应的 `evals/<phase>/`；第二阶段还包含 protocol.md。

Host 只加载所选阶段的一份完整 Prompt，不叠加 common 或另一阶段 Prompt。主 Agent 与接任候选遵循同一份阶段协议，每轮启动版本化为 `inputs/phase-instructions.md`，摘要进入 `startup.json`。初始 mini 的 system 直接加载这份内容；Gateway 在转发当前主身份、待接任候选的每次模型请求时，以同一版本化内容作为 instructions 前缀，保留其专属 instructions、消息和工具定义。主身份来自 Host 记录，候选来自 `/evolution/trials`，不依据模型在提示词里自称的角色。

普通 `/sessions` worker 包括局部测试 Agent，Host 不额外附加核心提示词；其完整提示词由主 Agent 编写。主身份交接后，接任候选继续得到核心协议，旧主身份不再获得新的附加层。部署准备检查不使用这份完整 auto research 协议。

启动和 resume 读取仓库当前文件；仅编辑源文件不会更新运行中的输入，运行中更换版本由 Host 独立发布。每个直接发布的 Agent 输入都有 Version，清单见 `/run/input/input-manifest.json`。其中 revision 是整套输入的快照编号，generation 是发布尝试序号，失败回滚也会递增；由 Host 自动维护。Gateway 用量记录 input_revision、各文件版本与摘要；历史内容保存在 run 的 input-revisions。主 Agent 与接任候选会获得当前阶段的目标、验收和完整 Prompt（第二阶段另含 protocol.md），旧对话作为历史保留。普通 worker 的专属任务仍由主 Agent 提供。

输入发布入口不接受 Agent 令牌。已发出的模型请求不变，后续请求采用新版本；Agent 连续读取多个输入文件时，前后必须都是 manifest.status=active，且 revision 和 generation 都相同。updating 时等待，标识变化后重新读取。热加载不重建现有 Python 对象、不切换模型或 Docker 镜像；bootstrap 运行参数及固定基础设施代码变化须经 Host 重启/恢复才应用。

人类反馈使用独立的 Host 控制入口，Agent 令牌不能调用。登记绑定正文与接收主身份，Gateway 接收文本而不是读取 Host 文件路径；正文或接收身份变化会使原确认失效。这些控制入口不是 Agent 可调用工具。

反馈在当前主身份下一次完整历史模型请求中作为 user message 加入，沿用 `.host/human-feedback/` 的副本、反馈 ID、摘要、投递事件与用量标记。已发出的请求和作业继续执行；登记成功表示等待投递，后续 `forwarded` 才表示带反馈的模型请求成功返回。源文件不会被自动监视；反馈不广播给其他 worker，也不自动移交给下一代主身份。此操作属于显式人工参与。

## 远程资产、输出与额度

部署方可配置只读 `/models`、`/datasets`；Agent 根据目录中的实际文件选择模型和数据。远程作业自己的 `/workspace` 与同一 worker 身份持久可写的 `/outputs` 保存在服务器项目的数据目录中。其他身份的输出不挂载，主身份交接不自动授予前任输出访问权；文件、版本或大文件交接须使用 Host 已支持的显式授权途径。

部署信息 remote_limits 保存实际并发、单作业超时和本地 run 文件与新增远程数据的合计额度。timeout_seconds 为 0 表示不限时，run_storage_bytes 为 0 表示不启用这项额度；只读公共模型与数据不计入新增额度。所有后继和辅助 worker 的新增远程数据合并计入同一 run；Host 每五秒报告本地 run 文件占用，远程 watcher 使用最近报告与远程占用之和。

额度检查在提交前执行，运行中的 watcher 每秒扫描，达到额度后停止自己的作业，保留文件和失败原因。并行写入及正常停止有延迟，可能短暂超过阈值；这是监测式额度，不是文件系统硬配额。收到 storage_limit 或 run storage limit reached 后保存状态并结束预算内任务，不以重复提交绕过额度。

## 新任务与 checkpoint 复用

阶段二通过 --run-harness 启动新任务时，Host 从所选保留 commit 复制代码、文件和持久记忆，在新副本中将 checkpoint.json 的消息列表初始化为当前阶段目标。原 checkpoint、轨迹及原保留 commit 不改；历史文件按授权放在只读 history 中，按需检索。启动记录 task_context 保存源 checkpoint 摘要、旧消息数与新会话消息数。

当前新任务入口支持 checkpoint.json 为 JSON 消息列表的 Harness，并继续使用其原启动命令。未知格式会拒绝启动，须由 Harness 提供适配入口；Host 不猜测私有状态格式或代写记忆整理。普通 --resume-from 仍保留原会话、原身份和完整上下文。

## 同卡多作业与显存预留

memory_shared 模式允许多个独立 Docker 作业共享同一张 GPU，文件、身份与输出仍各自隔离。Agent 不指定卡号；Host 根据显存余量和整机已使用卡数分配，max_gpus 约束本项目同时使用的物理卡数，max_jobs=0 时不按作业数量阻止新增任务。

共享模式下，`resource_busy` 的 `reason=no_eligible_gpu` 表示本次没有符合准入条件的卡，`gpu_allocation` 给出请求显存、活动卡数，以及逐卡实际占用、可用容量和排除原因；`reason=job_count_limit` 则表示达到作业数量上限。不要把两者都理解为整机没有空闲算力。成功 GPU 提交也返回分配报告。资源判断只描述提交时的快照；稍后可按事实重试，已经在 CPU 上运行的作业不会自动迁移到 GPU。

共享模式按剩余显存接纳任务，允许与外部进程同卡；已有显存占用和 GPU 利用率不会单独触发排除。Host 同时检查本项目活动卡数、每卡预留上限、整卡实际显存和尚未兑现的预留，并保留默认 2 GiB 缓冲。Agent 不能自行更改这些部署限制，也不能控制或回收外部作业。

POST /jobs 可选 gpu_memory_mb，表示预计显存峰值，单位 MiB。未提供时采用部署方的默认预留；小仿真可直接使用默认值，大模型加载/训练应提交合理的峰值估计。Host 同时检查每卡预留总额、实际显存和尚未兑现的预留，扣除缓冲。过大请求返回 resource_unavailable；暂时无余量返回 resource_busy。

最多同时使用的物理卡数仅统计本项目作业所在的卡；外部进程不消耗这个名额，其显存计入整卡实际占用。此模式共享计算与显存，不提供每作业硬显存隔离。GPU UUID、共享标志和预留量随作业保存；比较运行时间时记录同卡并发条件。

## 人类准备的离线模型资源

backbone、tokenizer 及相关预训练资源由人类提前准备、校验后只读提供。作业禁用网络，HF_HUB_OFFLINE 与 TRANSFORMERS_OFFLINE 固定为 1，缺项应记录并请求补齐，不自行下载。部署方可提供 /model-cache，其中的 Hugging Face 与 Torch 缓存只包含本项目已批准的资源。

模型附属配置若引用原始资源目录，Host 可把同一只读模型目录同时挂载到原路径；这只开放既定模型资产，不开放原路径的其他文件。USER/LOGNAME 使用固定的 agent，HOME 位于可写临时目录；远程新建作业默认上限为16 CPU、64 GiB内存和1024个进程与线程，禁止额外swap；实际限额以部署信息和作业记录为准。临时文件与PyTorch编译等可写缓存使用独立 `/job-tmp`，由Host挂载执行服务器数据盘目录，计入本轮存储并在作业退出后回收。`/tmp`只读；需保留和恢复的checkpoint、数据与报告写入 `/outputs` 或 `/workspace`。旧作业不自动更换挂载。


## 大文件只读交接

训练 checkpoint、数据集和视频在执行端封存；模型请求只传引用与摘要。原作业输出仍归原身份，封存不会开放整个输出目录。

1. 先让写入选定文件的作业完成或停止写入，再 `POST /jobs/{job_id}/exports`，例如 `{"location":"outputs","path":"checkpoints/best"}`。location 可用 outputs 或 workspace，path 是本身份作业内的相对路径。只接受普通文件和目录，拒绝符号链接与特殊文件。
2. 返回 export_id 和 preparing。用 `GET /jobs/exports/{export_id}` 等待 ready；失败返回 failed 与 error。封存使用独立进程，不占 GPU。每个文件复制并计算 SHA256，源文件在封存期间改变则失败；ready 后源文件再改变不影响快照。`GET /jobs/exports/{export_id}/manifest` 返回相对路径、字节数与逐文件 SHA256。`GET /jobs/exports` 仅列出本身份获授权的快照。
3. 创建普通 worker 或完整接任候选时，在 `/sessions` 或 `/evolution/trials` 请求中加入 `"read_only_exports":["export-..."]`。Host 验证主身份有读取权，并在新进程启动前授权。创建失败撤回授权。也可用 `POST /jobs/exports/{export_id}/grants {"session_id":"candidate-..."}` 授予自己直接创建的 worker；普通 worker 只能把自己可读的快照明确交付给当前主身份，不能发给同级或无关身份。
4. 获授权的 Agent 提交 `/jobs` 时加入 `"input_exports":["export-..."]`。执行端把每份快照挂载为只读 `/inputs/<export_id>/`，保留选定目录的相对结构；选单个文件时路径为 `/inputs/<export_id>/<文件名>`。本地 Agent Docker 不挂载这些执行端二进制文件，需要通过远程作业使用。未授权或未 ready 的引用拒绝提交，不分配作业资源。每个请求最多 64 个引用。
5. 接任主身份后可继续把已获授权的引用传给自己的 worker。退出、container 回收和同 run 恢复保留快照及授权。跨轮使用时，把选定 ID 写入自己的 continuation.json 顶层 remote_exports 数组；Host 保存前验证 ready 与可读性。之后显式 `--run-harness <该run>` 会把引用授权给新 run 的主身份及初始 Harness，无需人工复制大文件；新的模型上下文仍按阶段初始化。

例如保留声明：

```json
{"command":["python","agent.py","--resume"],"files":["agent.py","checkpoint.json","memory.json"],"remote_exports":["export-<32位十六进制ID>"]}
```

只保留引用，不把二进制加入 UTF-8 源码清单。原始快照与授权必须仍存在，且新 run 使用同一执行端资源根目录；缺失、删除或撤权会使启动明确失败。Agent 在交接材料中记录引用、用途、摘要和所需文件路径，不能期待 Host 替它选择 Policy 或数据版本。

`DELETE /jobs/exports/{export_id}/grants/{session_id}` 撤回授权；被授权身份仍有作业挂载时拒绝。`DELETE /jobs/exports/{export_id}` 删除自己创建的快照，要求调用身份是原始所有者，且没有活动作业挂载；不会删除源输出。快照默认保留，仍被 retained 结果引用时不要删除。封存副本计入来源 run 的新增存储额度，后续只读引用不再复制计费；在新 run 中生成的可写结果计入新 run。封存前、复制期间及发布 ready 前检查额度，失败清理未发布的数据并保存错误。并发写入仍可能短暂超过监测额度。


## 人类反馈与源码版本

部署方可在恢复时或运行期间显式登记人类反馈。Host 把它作为独立 user message 加入登记时主身份的下一次完整历史模型请求，原历史与 checkpoint 不改写。后续请求保持同一反馈位置，输入历史压缩或重建时重新定位并留下事件；具体文本、摘要和反馈 ID 在 Host 私有目录保存。反馈以独立输入投递，具体处理见本阶段 phase-instructions.md；普通文件和工具输出不因此变为反馈入口。该入口不自动广播给测试 worker 或后继身份。

远程镜像提供 git 与只读 /opt/a3/.git，git -C /opt/a3 rev-parse HEAD 返回实际 A3 源码提交。/opt/a3/source-revision.json 保存镜像准备时的文件摘要及源工作区 dirty 标记，未提交改动需结合摘要和 git diff 核对；源码版本不代表训练权重版本。导出的浅仓库不复制 Host 的 remote、认证配置或 hooks。运行镜像中的文件可能是完整源码仓库的部署子集，git diff 应按实际路径解读；不要修改只读基线。
