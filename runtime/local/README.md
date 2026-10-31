# Host提供的执行与验收入口

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

候选由模型驱动只说明执行主体成立。A/B/C/D 仍分别需要冻结标准要求的真实能力、
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
固定限制为 2 CPU、2 GiB 内存、256 个进程、512 MiB 临时目录，同时最多两个活动候选容器。
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
作业根文件系统只读、无网络，只挂载自己的工作区，无 SSH、Docker socket 或 gateway 令牌。
任务结束后通过 `GET /jobs/{job_id}` 查询状态，通过
`GET /jobs/{job_id}/artifacts?path=relative_path` 取回产物；取消为 `DELETE /jobs/{job_id}`。
资源忙时稍后重试。长任务先提交再查询，避免单条本地命令超时。

部署限制：每个作业 2 CPU、8 GiB 内存、最多一张 GPU、900 秒时限，同时最多两个作业。
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

初始 SWE 只启动一次。它退出后，Host 保持已经启动的后继、Gateway 和资源服务可用，等待所有 worker 停止。Host 不定时重建 Agent，不因未完成或错误重新派发任务，不提供人工审查反馈。后续进程与交接由 SWE 自己安排。

全部 worker 停止或启动器被用户中断后，Host 清理本轮资源。清理失败保存 `cleanup-errors.json` 供复核。外部观察者在运行中只观察和按授权开始/停止循环；实际验收复核等整个系统停止后进行。

### 整轮结束与未完成交接

当前构建者使用 `/workspace/run/run-outcome.json` 声明终态。验收细节与证据格式仍由 Agent 组织；这份清单在所有 worker 停止后用于记录结果：

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

验收项取自冻结 `acceptance.md` 的字母标题，所有必需项均为 `passed`、引用文件在本工作区实际存在且非空，并有有效 `continuation.json`，才能声明完成。路径相对 `/workspace/run/`。先将选择的后继证据与入口按授权交接到该目录；Host 不替 Agent 选择候选。Host 检查协议和文件可达性，实际能力仍按冻结标准复核。

未完成时可写 `{"status":"continue","reason":"尚未完成的能力与已有进展"}`，或仅保存进展后提交本回合。提交前须自行安排后继；Host 只等待现有后继，不开启新回合。全部 worker 已停止但未完成时，记录 incomplete。

确需人工处理的外部阻塞使用 `status: blocked` 与 `external_blocker`，字段为 `kind`、`requires_human: true`、`reason`、`required_action`、`evidence`。允许的类别是 `authentication`、`permission`、`unavailable_service` 和 `missing_resource`；同样需要可达证据。候选名额、作业等待、无收益候选和待完成验收都应继续处理。

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
远程 `resource_busy` 会记录容量等待；远程调用总耗时含 SSH 与 Controller 处理，当前尚未分拆模型加载、实际计算等内部耗时。

保留的阶段一 `research` 入口仍使用原有 Gateway 路径清单。接入新增 `/versions` 和 `/checks/comparison` 操作由 SWE Agent 在后续候选中自主完成；Host 已准备好相应授权与检查。

## 运行结束与自动接续

主 Harness 在自己的 `/workspace/continuation.json` 声明下一轮保留结果；初始构建者使用 `/workspace/run/continuation.json`。内容为启动命令和完整的相对文件清单：

```json
{"command": ["python", "next_agent.py"], "files": ["next_agent.py", "memory/lessons.json"]}
```

文件按 UTF-8 传递，沿用 128 个文件、20 MiB 的限制。Agent 自己选择保留版本和需要的启动状态；Host 只保存快照，并写本轮 `retained.json`。大型或二进制经验材料可保存在历史中，由 Harness 按需恢复。下次外部 `--run-harness <run目录>` 和内部 `/sessions` 使用同一候选执行层，无需人工整理接续包。

从旧运行启动的新主会话可以读取 `/run/input/previous-run.json` 与 `/run/input/history/index.json`。历史来自明确选择的前序运行，已过滤旧认证与私密推理；其中内容按待核查数据处理。仅主会话获得完整只读历史；它创建的子会话使用普通输入，所需材料由主会话显式交接。

## 上下文 checkpoint 与 resume

初始 mini 的 trajectory 原子写入，恢复时直接加载原消息，不重建 system/user 消息，不回注外部审查。冻结的 bootstrap 与执行说明随原目标保留。主动提交的会话不恢复；未保存结果的工具调用拒绝自动重放。

生成的 Harness 用 `resume.json` 声明包含 checkpoint 的源码清单及恢复命令。Host 不把 continuation 入口当作 resume；缺失 checkpoint 入口时拒绝启动。新 Docker 身份不自动取得旧 Gateway scope 或作业权限，此接口不承诺恢复任意进程内存与旧资源身份。
