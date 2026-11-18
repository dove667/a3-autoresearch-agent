# 远程进程后端

受信任的 Linux 执行端直接启动研究命令。本地 Host 和原生 Harness 管理模型与工具循环，远程执行训练、仿真、渲染和评估。

## 配置

在私有 `.env` 配置已有 Python 环境和独立数据设备中的路径：

```dotenv
A3_REMOTE_BACKEND='process'
A3_REMOTE_MANAGED_JOBS='1'
A3_REMOTE_MANAGED_GPU_MEMORY_MB='8192'
A3_REMOTE_DATA_ROOT='/data/project'
A3_REMOTE_RUN_ROOT='/data/project/remote-runtime'
A3_REMOTE_TMP_ROOT='/data/project/temporary'
A3_REMOTE_PROCESS_PYTHON='/data/project/a3/.venv/bin/python'
A3_REMOTE_A3_ROOT='/data/project/a3'
A3_REMOTE_HOST_MEMORY_RESERVE_MB='32768'
A3_REMOTE_HOST_JOB_MEMORY_MB='8192'
A3_REMOTE_HOST_CPU_RESERVE='2'
A3_REMOTE_HOST_JOB_CPUS='2'
A3_REMOTE_DISK_RESERVE_BYTES='68719476736'
A3_REMOTE_RUN_STORAGE_BYTES='150000000000'
A3_REMOTE_MAX_JOBS='0'
```

示例预算由部署者按真实容量调整。Controller、运行目录、解释器、资产、快照与临时目录都应在数据设备；process 健康检查和提交拒绝系统盘管理目录及数据根以外的路径。Python 环境由部署方预先安装并验证。`A3_REMOTE_IMAGE` 仅用于 Docker，新进程作业不使用它。

进程作业使用独立的 `PATH`，其中解释器所在的目录排在首位。训练依赖 `ffprobe` 和 `ffmpeg` 时，部署方可在该目录为两个命令分别创建指向现有可执行文件的符号链接，并通过一条实际的 CPU 作业验证视频探测和解码。

## 源码提交与回执

Agent 使用 `job_submit` 附加当前共享工作区 `scripts/` 中的 UTF-8 研究源码，工具计算文件 SHA256。`destination` 可指定作业中的相对目标路径，省略时保留源路径。例如：

```json
{
  "command": ["python", "runner.py"],
  "gpu": false,
  "attachments": [{"path": "scripts/act/baseline/evaluation/runner.py", "destination": "runner.py"}]
}
```

模型使用 `job_submit` 或 `job_prepare` 的 attachments。Host 内部将它转换为带 SHA256 的 workspace_files 并按身份读取核对，拒绝符号链接、重复目标与文件／目录冲突。附件最多16个、每个1 MiB、合计8 MiB，源码与实验配置采用同一入口。数据集和权重通过 input_exports 引用。HTTP POST /jobs 是基础设施内部接口。

响应中的 `source_files` 与 Host 的 `job-inputs.jsonl` 保存实际源路径、目标、摘要和字节数，供报告关联作业版本。`submission_unknown` 保留已分配的 job ID，先查现有状态和产物。源码传递不创建额外研究身份。

## 评测入口与步数预算

A3 的闭环评测统一由 `benchmark` 工具提交，不接受手拼的评测命令。工具固定任务预算——2×5 为 500 步、2×10 为 3200 步——并生成作业命令、输出目录与附带的 `benchmark_runner.py`。部署的 `examples/benchmark_cookie_batch.py` 只接受一个 `--max-steps` 且默认 1000，各任务专用包装脚本才分别使用 500 与 3200；把短预算用在 2×10 上会让回合在搬完第一排时耗尽步数，通过率与入盒数都不再反映策略表现。

`job_prepare` 与 `job_submit` 在提交前解析命令中的 `--task` 与 `--max-steps`：低于任务预算或省略而依赖入口默认值的评测命令会被拒绝，并在提示中给出该任务的正确数值。确实要做更长预算实验时显式提高 `max_steps`。工具返回 `report`，即结果 JSON 的 outputs 相对路径，可直接用 `job_artifact` 读取，不必猜测文件名。一格的多个 report 用 `benchmark_summary` 合并：它按每回合的任务上限截断入盒数、把原生通过单独计数，并在预算、动作块、prompt 风格、checkpoint 或 seed 不一致时点名——手工求和会把超过上限的回合和不同协议的分段算进同一个数。

## A3 接口查询

`a3_api(symbols=[...])` 读取部署的 A3 源码，返回核心函数的文件、行号与完整签名，以及采集和评测 CLI 的开关与默认值。它只读文件，不 import 模块，因此不会因副作用失败。用途是替代“先跑挂、再读栈、再改脚本”：`train_act` 的模块位置、`aggregate_datasets` 的参数名、`merge_cookie_datasets` 对双 root 的要求都属于会漂移的契约。

## 失败分类与重试

`job_status` 与 `job_wait` 的诊断带 `retry` 字段。只有 `killed` 与 `distributed_failure` 允许原样重做一个请求：前者是调度或主机终止，后者是端口或 NCCL 冲突，都与脚本内容无关。其余分类明确标为不可重做——同样的代码再跑一次只会再失败一次，还要再占一次 GPU。

## 开局体检

窗口启动时，Host 在会话可以接收作业之后立刻提交两个 CPU 作业：`startup-a3_api`（A3 接口快照）与 `startup-dataset_audit`（数据集体检，仅在配置了 `A3_STARTUP_DATASET` 时）。提交结果写入 `trajectory/startup-checks.json`，作业编号同时打印在启动日志里。这里任何失败只记录、不抛错：Controller 不配合时窗口照常开始。

## 数据集体检

`dataset_audit(dataset_root)` 在 CPU 作业里调用部署的 A3 审计函数，并补上审计本身不提供的分布信息：每种任务布局（2×5／2×10）的回合数、逐条指令的计数、成功标记、seed 数、总帧数和存储动作模式。它对三种情况给出警告——`complete_demonstrations` 为 false、数据集只有一种布局、元数据计数与 `meta/info.json` 不一致。新增这些检查的原因是现写探针会失败：CLI 把相对 `--root` 解析到项目根，曾把 37 条已保存回合报成 0。

## 资源调度

不设置单作业 CPU、RAM 或 PID 硬上限。提交时检查整机负载、可用内存、待用 RAM 估计和数据盘余量；CPU／内存估计用于限制同时接收的作业数，不限制已运行进程的实际占用。GPU 显存预算、物理卡数量和共享租约继续生效，与旧 Docker 作业共用调度锁和记录。`max_jobs=0` 取消固定数量上限，仍经过整机准入。

启用 `A3_REMOTE_MANAGED_JOBS=1` 后，提交立即获得持久 job ID 和 queued 状态。独立服务器 dispatcher 每10秒检查待执行任务；CPU、内存、GPU 或数据盘暂时不足时保持原任务排队，资源满足后沿用原 ID 启动。dispatcher 不调用模型，Host 断网不影响队列和已启动作业。

共享 GPU 作业按单作业预算预留显存。显式 `gpu_memory_mb` 保留原值；未指定时采用 `A3_REMOTE_MANAGED_GPU_MEMORY_MB`，默认 8192 MiB，并受配置的单卡容量约束。Controller 只预留该预算，余下显存继续容纳其他作业；Agent 无需计算服务器当前空闲显存。超配置容量的显式请求在上传前返回 `resource_unavailable`。请求超过所有允许设备的物理容量（扣除缓冲）时，已排队作业明确失败并保留原因；临时占用和项目同时用卡数量限制仍等待。episode 执行器在分配预算内调整并发。Controller 的配置、诊断与运维属于部署者。

队列和输入保存于数据盘，取消 queued 作业后不会执行；活动管理索引只保留非终态作业，历史元数据保留。空闲一分钟后 dispatcher 退出，下次提交自动唤起，带进程身份验证和单实例锁。待执行队列可从同一数据目录恢复；服务器重启导致实际执行中断时不重放未知进度。启用前已运行的作业保持原 watcher。队列没有额外模型或 Agent 会话。

每次准入在共享调度锁内取得一次活动作业与 GPU 状态，直接用于资源检查和启动；排队层只补充请求预算，不重复扫描历史或采样显存。既有作业的实际预算保持原值，历史 Docker 与 process 作业共同参与准入。旧排队记录在下次派发时使用保存的显式预算或默认预算，不重新提交或更换 job ID。

未启用托管执行时，资源不足仍返回 `resource_busy`。进程 watcher 每5秒检查存储预算和整盘余量；触及阈值停止本作业、保留文件。CPU／内存准入使用整机估计，研究程序应保持有界数据缓存与线程数。共享缓存、临时数据、完整进程日志和归档计入 run 预算。入队前的容量不足不接受新输入，交由操作者处理；排队不扩大存储额度。

## 路径与权限

提交和状态响应返回 `backend:process`、`paths` 和 `input_paths`。工作区的 `a3-job-context.json` 提供同一映射。常用环境变量：

| 变量 | 用途 |
| --- | --- |
| `A3_PYTHON`、`A3_A3` | 已配置 Python 与 A3 源码根目录 |
| `A3_WORKSPACE`、`A3_OUTPUTS` | 本作业源码与本身份持久输出 |
| `A3_SHARED_CACHE` | 同 run 共享预处理缓存，计入额度 |
| `A3_TEMPORARY` | 本作业数据盘临时目录 |
| `A3_MODELS`、`A3_DATASETS` | 部署者配置的公共资产 |
| `A3_INPUT_EXPORTS_JSON` | export ID 到已授权封存目录的 JSON 映射 |
| `A3_EPISODE_EXECUTOR`、`A3_JOB_TELEMETRY` | 固定 episode 执行器与 GPU 遥测路径 |

`python`／`python3` 命令使用部署解释器。作业从真实 workspace 启动，命令数组不展开 shell 变量；程序需用 `os.environ` 读取路径。不重写研究源码、不创建 `/workspace`、`/outputs` 等全局别名。旧数据清单含容器路径时，Agent 根据实际 export 来源适配读取，并保留原始证据。

HOME、TMPDIR、XDG、模型、编译等常见缓存全部指向数据盘。程序显式写入 `/tmp` 或系统盘仍可能成功，因此必须遵守数据盘写入约定。此模式使用执行账号的文件系统和网络权限，不提供 Docker 安全隔离；适用于已信任的研究代码。不得给远程作业传入 Host 模型、Gateway 或 SSH 凭据，也不得修改公共资产、封存快照或其他任务。

## 渐进切换

配置只作用于新提交。旧 Docker 作业继续使用原环境、路径、watcher 和资源预算，完成后按原策略归档并回收。其旧 run 存储阈值在退出前仍然有效；迁移时新增数据必须兼顾在途旧 watcher。Docker daemon 和非本项目容器不删除。

Host 中断或恢复不重新执行远程命令。进程状态用 PID、启动时刻和系统启动标识核对；取消通过 pidfd 对验证后的本作业进程及后代发信号，日志和输出保留。未知退出码不会推断成功。共享 GPU 的模型加载竞争重试沿用同一逻辑编号、命令和输出，最多两次，条件与 Docker 相同；托管作业重试暂时无资源时在服务器等待，不要求 Agent 重提。普通代码或训练 OOM 不重放。

共享缓存的内容寻址、解码版本、并发锁、原子发布和有效性检查由研究 Agent 实现。使用数据等待、缓存命中、解码次数、写盘量和有效训练吞吐评估改进，避免只看容器数量或瞬时 GPU 利用率。

## 提交后等待结果

Agent 提交任务和科研参数，Controller 与 episode 执行器负责资源、生命周期和并发调整。Agent 通过 `job_wait` 等待最多64个指定作业，超时后返回；底层 Host 等待请求由 Harness 续接。Host 复用 30 秒活动状态缓存、批量读取 watcher 元数据，终态缓存 5 分钟。接口返回简短终态或通知，模型在目标终态、总超时、输入变化或操作者通知时处理结果。研究代码错误由 Agent 分析；Controller 异常由操作者检查服务器记录。托管模式的部署说明和常规作业结果不展示服务器资源明细，具体请求见[执行说明](../../prompt/execution.md)。

固定 Host 监督每 15 秒观察一次会话生命周期，仅状态变化追加监督事件；不驱动研究步骤或重启 Agent。作业 watcher 继续独立运行，不依赖 Agent 查询来推进训练或完成归档。
