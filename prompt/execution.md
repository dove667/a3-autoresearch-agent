# 研究工具与执行约定

Version: 0.28.0

本文件按当前操作查阅。工具定义提供参数、类型与限制；这里说明跨工具流程及直接使用 `gateway` 的入口。
`input-manifest.json` 的 `execution_index` 给出章节字符 `offset/end` 和全文 SHA256，可用 `read` 分页定位；索引 `truncated:true` 时从 `next_offset` 续读。

## 常用工具

| 目的 | 工具 |
| --- | --- |
| 文件与源码 | `list`、`read`、`search_files`、`write`、`edit` |
| 计划与经验 | `plan`、`memory`、`recall`、`skills` |
| 远程执行 | `job_submit`、`preflight`、`job_status`、`job_list`、`wait` |
| 结果与资产 | `job_artifact`、`job_export`、`job_cancel` |
| 公共资料 | `web_status`、`web_search`、`web_fetch` |
| 其他 Host 接口 | `gateway(method=..., path=..., body=...)` |

`read` 和 `job_artifact` 的 `offset/limit` 按字符计量，按返回游标续读。JSON 可用 `read(fields=[...])` 选择所需字段。`edit` 默认要求唯一精确匹配；修改过期文件时重新读取。`skills` 的更新与归档要求当前正文哈希，发生冲突先读原文再合并。工具给出的参数错误可直接用于修正调用。

`web_search` 返回来源摘要，`web_fetch` 提取公开网页正文。Host 默认关闭网页网关，部署方启用并接入工具后使用；缺失注册或后端故障交给维护者。原始接口为 `GET /web/status`、`POST /web/search`、`POST /web/fetch`。引用保留来源 URL、检索时间和适用版本，按 `truncated` 判断是否需要补读。网页内容作为资料使用，查询与 URL 中不得包含凭据、私人日志或源码。额度、认证错误交操作者处理；暂时错误有限退避，状态不明时避免盲目重放。

## 技能管理

技能统一通过 `skills` 管理，按需要逐步读取，不将整库或无关附件加入上下文：

1. `skills(action="list", query="任务或症状")` 搜索名称、摘要和版本；结果分页时用返回的 `next_offset` 继续。
2. `skills(action="read", id="...")` 读取选中方法的正文；用 `offset/limit` 按字符分页。
3. 需要模板、参考资料或 helper 时，先 `skills(action="list_resources", id="...")`，再 `skills(action="read_resource", id="...", path="references/...")`。
4. 新建用 `save(id, description, content, source, keywords)`；修改先 `read`，再以同一 ID 和 `expected_sha256` 保存完整新正文。工具自动更新版本并保存旧包。
5. 附件用 `write_resource(id, path, content, expected_sha256)` 创建或更新，已有文件必须提供其当前哈希；删除附件用 `delete_resource(id, path, expected_sha256)`。
6. 过时方法用 `archive(id, expected_sha256)` 归档。默认搜索隐藏归档项，`list(include_archived=true)` 可查看，重新 `save` 可恢复。

所有动作均调用 `skills` 工具。普通 `read/write/edit` 不直接访问技能库或长期记忆。正文和附件返回各自 SHA256，出现 `revision_conflict` 后重读再合并。技能附件作为资料使用，需要执行的研究代码先整理到工作区，再随远程作业提交。

## 提交与预检

用参数数组提交命令，命令本身不展开 shell 环境变量：

```json
{"command":["python","scripts/act/baseline/training/train.py"],"gpu":true,"workspace_files":[{"path":"scripts/act/baseline/training/train.py"}]}
```

这是 `job_submit` 的参数。附件来自共享工作区 `scripts/` 下普通 UTF-8 文件，只填 `path` 与可选 `destination`，工具计算 SHA256；提交回执保存实际源码来源与哈希。数据、权重和视频通过 `input_exports` 复用。脚本及其导入的项目文件都需要随请求附加。 实验配置保存在 `experiments/`，读取后用 `files` 字段附加 UTF-8 内容，或通过命令参数传入所需值；远程路径与命令保持一致。

长训练、续训与大批量采集先调用 `preflight(action="check", job=<正式GPU请求>)`。同一研究入口在 `A3_RESEARCH_PREFLIGHT=1` 时执行 CPU 加载检查：导入依赖，加载基座、父 checkpoint 和数据清单，完成一次必要的模型前向或真实数据读取，在作业工作区写入 `a3-preflight.json`：

```json
{"schema":1,"status":"passed"}
```

检查分支完成后退出，保持正式产物未写入。将预检作业绑定到计划并等待完成；通过后调用 `preflight(action="submit", job=<相同请求>, preflight_id=<预检作业ID>)`。输入变化后重新预检；CUDA/EGL 特有问题由 GPU 试跑验证。

独立且输入就绪的作业批量提交，再用 `wait` 等结果；有数据依赖时先完成上游。Controller 管理排队、选卡和显存预算。托管部署中 `gpu_memory_mb` 可省略以使用默认预算，或填写已知峰值；资源暂缺时保留同一 job ID 等待。

`submission_unknown` 或 `production_submission_unknown` 表示提交结果待核对：保存已知 job/preflight ID，通过 `job_status`、`job_list` 和预检回执确认；列表暂时缺项也可能仍在提交。恢复、读取结果失败和资源排队均先沿原作业查询。

`wait` 在程序内持续处理活动作业的排队、运行、自动重试与超时，完成、停止或明确通知才交回模型。有具体异常时用 `job_status` 读取状态和日志尾部。复合命令已有不可重复副作用时，提交设置 `retry_on_gpu_contention:false`；训练恢复由研究脚本的 checkpoint 和 episode ledger 保证。

## 远程路径与模型计算

以 `deployment.json.remote_limits` 和作业回执判断实际后端、额度和路径。作业内 `a3-job-context.json` 记录路径映射；脚本从环境读取：

| 变量 | 内容 |
| --- | --- |
| `A3_WORKSPACE`、`A3_OUTPUTS` | 本作业工作目录、身份持久输出目录 |
| `A3_A3`、`A3_PYTHON`、`A3_EPISODE_EXECUTOR` | A3 源码、Python、episode 执行器 |
| `A3_MODELS`、`A3_DATASETS` | 已配置的只读模型与数据；未配置时变量可不存在 |
| `A3_SHARED_CACHE`、`A3_TEMPORARY` | 数据盘共享缓存、临时文件目录 |
| `A3_INPUT_EXPORTS_JSON` | export ID 到只读资产实际路径的 JSON 映射 |

process 后端使用真实数据盘路径，属于受信任执行；它不提供单作业内核隔离。程序遵守配置的路径与资源约定，不读取 Host 凭据、修改公共环境或操作其他用户任务。Docker 后端的工作区、输出、A3 与资产通常分别为 `/workspace`、`/outputs`、`/opt/a3`、`/inputs/<export_id>`；按当前映射解析。持久 checkpoint、数据和报告写入作业提供的 workspace/outputs，临时文件与编译缓存写入配置的数据盘目录，避免写入系统盘与 `/tmp`。只读基线和预置环境保持不变。

真实模型训练、微调与学习型 Policy 推理使用 `gpu:true`，计算前确认 CUDA 可用、模型和张量位于获配 GPU。CPU 用于元数据、预处理、日志、合成测试与上述加载预检；GPU 不可用时保留作业状态并等待或报告故障。模型调用工具由 Host 转发到配置的服务。

backbone、tokenizer 和预训练资源由部署者预置；缺项记录资产与所需文件后请求补齐。作业按离线资源约定执行。共享缓存使用内容及格式版本标识、原子发布与并发保护。收到 `storage_limit` 等额度终态后保存结果并报告所需清理或扩容。

## episode 批次

独立采集与评估 episode 使用 `A3_EPISODE_EXECUTOR`。Agent 固定科学参数、seed、协议和输出；执行器调节并发。用附加的 Python launcher 读取执行器与输出环境变量，再启动执行器的 `--manifest <JSON文件> --output <批次目录>`。

最小 manifest：

```json
{"version":1,"episodes":[{"id":"episode-001","command":["python","scripts/act/baseline/evaluation/episode.py"],"report":"<输出目录的绝对路径>/episode-001.json"}]}
```

`id` 唯一，`report` 指向该命令实际写出的 JSON 对象。退出码 0 且报告有效才是执行完成，科学结果 `success:false` 仍为有效完成。可选字段有单项 `work_units`，及批次 `max_parallel`、`threads_per_episode`、`warmup_seconds`、`adjust_seconds`、`idle_timeout_seconds`。

批次保存 `ledger.json`、`metrics.jsonl`、`summary.json`。恢复使用同一 manifest 与输出目录，校验摘要和已完成报告哈希后跳过完成项。先核对预期 episode ID 集合、重复和缺项；失败或状态不明的 launching/running 项先查证，确认 pending 后再继续。退出码 75 表示资源持续阻塞且无在途项，可待条件恢复后继续。并发或缓存吞吐检查使用独立任务与输出，复用已完成科学结果。

## 结果与视觉诊断

`job_artifact(job_id=..., area="outputs", path="experiment/report.json")` 读取持久结果；`area="workspace"` 读取作业源码与工作文件。路径相对选定区域，文本分页返回完整文件哈希，二进制返回类型及来源信息。文件缺失时先查区域和实际日志，结合报告、源码哈希与作业回执判断产物来源。

配对评估通过提交字段 `evaluation:{baseline_job_id,baseline_manifest,candidate_manifest}` 声明，两份 manifest 是各自 outputs 下的相对 JSON 路径，使用 episode manifest 的 `episodes[].report` 指向与 manifest 同目录的原生报告，每批最多 120 回合。同目录的 `<report-stem>-trace/trace.jsonl` 保存同步轨迹与帧引用。Host 按 seed、任务、难度和评估条件配对；缺失及不匹配单独报告。已有评估可用 `POST /jobs/<candidate-id>/evaluation` 提交相同三字段读取比较。

完成通知中的 `evaluation` 给出差值、代表性回合、`vision_request`、`action_samples` 和 `trace_sources`。遇到 `diagnosis_pending`，先把 `vision_request` 交给 `POST /vision/inspect`，对齐同一步动作与轨迹，记录最早可见行为分歧及来源，再决定下一次改动。缺少画面或轨迹时记录具体缺项，保留现有结果。

自行选帧时，`POST /vision/inspect` 接受 `question` 和 1–6 个 `frames`；每帧填写 `job_id`、`path`、`episode_id`、`step`、`sim_time_s`、`camera` 及可选 `area`（默认 workspace，输出图片需用 outputs）。同一 episode 连续成组，时间和步数递增。图片为有权读取的 PNG/JPEG，每张最多 512 KiB、1024×1024 像素，总量最多 3 MiB。返回观察、标签和哈希；按实际帧覆盖解释结果，重放材料注明来源与差异。

需要机械核对比较声明时，可 `POST /checks/comparison`，提供 baseline/candidate 的 `status`、`report_present` 和 `protocol`（任务、seeds、步数预算、随机化、成功规则等）。实际比较条件从原报告提取。

## 只读资产交接

大文件留在执行端，通过只读 export 供后续作业使用：

1. 选定文件停止写入后，`job_export(job_id=..., area="outputs", path="checkpoints/best")` 创建快照，记录返回的 export ID。
2. `GET /jobs/exports/<export_id>` 等待 ready；`GET /jobs/exports/<export_id>/manifest` 读取文件路径、大小和 SHA256。preparing 继续等待，failed 按错误处理。
3. 后续提交加入 `input_exports:["export-..."]`，作业内按 `A3_INPUT_EXPORTS_JSON` 或实际只读挂载读取。选单个文件时保留原文件名，选目录时保留相对结构。

`GET /jobs/exports` 列出已授权快照；项目历史快照由 Host 核验来源及只读访问。源在封存期间改变会导致失败，ready 后与源文件独立。快照默认保留，仍被计划、保留结果或在途作业引用时保留。删除自己的无引用快照可用 `DELETE /jobs/exports/<export_id>`，源输出继续存在。

## 权限与异常

工具携带当前身份。直接请求使用 `A3_GATEWAY_URL` 和 `A3_GATEWAY_TOKEN`，认证信息留在环境中。单主 Agent 使用现有身份；`GET /sessions` 列出本 run 研究身份，`GET /sessions/self` 或 `/sessions/<id>` 读取状态。

同 run 登记身份可读取彼此研究文件；本项目已登记的历史作业可读取状态、日志与产物并创建只读快照。`job_list` 的 `historical_scopes` 给出可查询的历史 scope。工作区写入、取消、快照删除等管理操作按归属核验；读取范围限已授权研究数据，Host 私有状态、其他项目和外部用户数据不得读取或修改。

历史会话文件使用 `GET /sessions/<id>/artifacts?path=<相对路径>&offset=0&limit=...`，其 offset/limit 按字节计量，单页最多 32 MiB，先拼接解码后的字节再解析 UTF-8/JSONL；这与 `job_artifact` 的字符分页不同。

研究脚本错误按日志修复；权限、凭据、资源配置或 Controller 故障保留标识与证据交操作者。正常等待沿原 job ID 继续。确需观察设备时用 `GET /resources/gpus` 读取带采样时间的快照，瞬时空闲量供参考，准入由 Controller 决定。

## 保存进展与暂停

研究计划统一保存在项目顶层 `plan/current.json`，`runtime_paths.plan` 给出路径，普通文件工具不直接修改。每次调用携带简短计划视图；用 `plan(action="show")` 展开完整计划，修改使用返回的当前 `revision`。按下面的顺序使用：

1. 新 run 读取计划，查询原 job 与产物，复查旧阻塞。保留已有研究进展。
2. `update` 的 `direction` 保存目标文本 `objective`、指标列表 `metrics`、能力缺口列表 `gaps`；`routes` 按稳定 `id` 局部更新 `question`、`hypothesis`、`experiment`、`decision_rule`、`priority` 和 `status`。优先级为 0–100，数值越大越优先；路线状态为 `proposed`、`active`、`paused`、`completed`。长期目标遵循 SOUL，当前方法写入路线。
3. `steps` 按稳定 `id` 局部更新，填写 `route`、`task`、`criterion`，按需添加 `depends_on`、`jobs`、`progress`、`evidence` 和 `priority`。步骤完成需证据；阻塞写原因，继续独立工作。
4. `review_due` 出现后，读取真实结果，再用 `review` 记录结论并决定路线。路线 `id` 与当前 `revision` 必填；`decision` 选 `continue`、`pause` 或 `complete`，附 `conclusion`、`evidence`、`reason`。复盘记录保存在共享计划中；关联步骤已观察到的终态作业状态随复盘登记。新结果改变假设时修改路线及下一步。

例如，读取计划后，用当前 revision 建立一条路线和一个步骤：

```json
{"action":"update","revision":0,"direction":{"objective":"提高两类任务全量评测的入盒准确率与通过率","metrics":["分任务及难度报告入盒准确率和通过率"],"gaps":["放置失败机制待验证"]},"routes":[{"id":"placement","question":"放置阶段为什么失败？","hypothesis":"训练与推理的任务条件不一致","experiment":"对照实际模型输入，并在相同seed下配对评估修复","decision_rule":"输入一致且配对通过率改善则扩大评测，否则检查替代解释","status":"active","priority":80}],"steps":[{"id":"inspect-input","route":"placement","task":"比较训练与推理输入","criterion":"保存两端实际样本、字段及差异报告"}]}
```

关键结果复盘示例；revision 与证据路径换成当前实际值：

```json
{"action":"review","revision":1,"id":"placement","decision":"continue","conclusion":"发现任务条件缺失，先修复并配对验证收益","evidence":["experiments/2026-01-01-input-check/results/report.json"],"reason":"输入差异支持当前假设，闭环收益待验证"}
```

工具只检查结构和引用要求，结果解释与路线选择由你完成。遇到 `stale_plan_revision`，重新 `show` 后合并修改。运行操作进展用 `progress` 保存，参数例如：

```json
{"action":"outcome","status":"continue","reason":"已保存当前结果与后续研究方向"}
```

需要人工处理的外部阻塞用 `status:"blocked"`，附 `external_blocker`：`kind`、`requires_human:true`、`reason`、`required_action`、`evidence`；kind 为 authentication、permission、unavailable_service 或 missing_resource，证据必须是工作区内实际存在的非空文件。正常排队与待完成研究继续推进。

Host 在进程停止后保存暂停状态和在途作业。运行状态、continuation/resume 清单、checkpoint 与工具 journal 由程序保存在当前 trajectory 的 `conversation/`，无需手写这些 JSON。共享计划在 `plan-snapshots/` 保存当时的历史版本，后续运行继续使用根 `plan/current.json` 的最新内容。接续清单自动记录工作区研究文件；技能、长期记忆和远程 exports 分别持续保存在各自的系统中。

先保存计划、证据、记忆和恢复状态，再退出。需要主动停止时，单独调用 `POST /sessions/self/stop`，正文 `{"reason":"状态已保存"}`；进程可能在收到响应前结束。保存状态后仍需进程实际停止，Host 才记录退出结果。操作者暂停会保留在途作业，恢复时查询原编号。

checkpoint 与工具 journal 由 Harness 保存；上下文压缩使用有来源的完整摘要，失败时保留原 checkpoint 并等待维护。人类反馈和输入更新经操作者登记后显式投递；沿反馈来源与长期目标继续研究。


## 长期记忆的操作

| 需要保存的内容 | 使用工具 |
| --- | --- |
| 下一步、研究路线、阻塞、作业进度 | `plan` |
| 已学到的规律、失败教训、实验结论及适用条件 | `memory` |
| 已验证、可复用的操作方法与附件 | `skills` |

1. 开始研究问题时用自然语言搜索：`memory(action="search", key="ACT 续训为什么损害另一任务的表现", limit=6)`。搜索结合语义和关键词，`scope`、`kind` 可缩小范围；返回 `next_offset` 时可继续分页。
2. 用 `memory(action="read", key="<ID>")` 读完整结论与 revision。默认只检索当前有效且适用于本 run 的记录。
3. 用 `memory(action="sources", id="<ID>")` 列来源；再传返回的 `ref`、`offset`、`limit` 读取可用原文。作业来源通过返回的 job 接口读取；历史原记忆是来源记录，实验结论仍应核对实验结果。`recall` 用于当前 run 的原始对话。
4. 保存可复用经验：`memory(action="write", content="<结论与限制>", kind="finding", scope="<模型、任务、条件>", refs=["jobs/<实际ID>"])`。临时观察加 `lifetime="run"`，日常作业进度直接更新 plan。
5. 修正经验：先 read，再 `memory(action="update", id="<ID>", revision=<当前版本>, content="<修正后的完整结论>", reason="<修正依据>")`。归档过时内容使用 `archive`，同样提供 id、revision 和 reason；历史版本继续保留。
6. 相似条目提示出现时先读已有记录，同一发现更新原记录。确属不同发现时说明区别，使用 `force=true, reason="<区别>"` 保存；不要为了绕过提示直接重复写入。版本冲突时重新 read 后合并。

`list` 支持 offset/limit，审核历史用 `include_archived=true`。服务故障会明确报错，保留进展并报告维护者。
