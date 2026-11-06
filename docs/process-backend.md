# 远程进程后端

受信任的 Linux 执行端可直接启动研究命令，不需要远程 Docker。模型和 Agent 仍在原来的本地 Host／候选执行链中，远程只运行训练、仿真、渲染和评估。

## 配置

在私有 `.env` 配置已有 Python 环境和独立数据设备中的路径：

```dotenv
A3_REMOTE_BACKEND='process'
A3_REMOTE_MANAGED_JOBS='1'
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

## 资源调度

不设置单作业 CPU、RAM 或 PID 硬上限。提交时检查整机负载、可用内存、待用 RAM 估计和数据盘余量；CPU／内存估计用于限制同时接收的作业数，不限制已运行进程的实际占用。GPU 显存预算、物理卡数量和共享租约继续生效，与旧 Docker 作业共用调度锁和记录。`max_jobs=0` 取消固定数量上限，仍经过整机准入。

启用 `A3_REMOTE_MANAGED_JOBS=1` 后，提交立即获得持久 job ID 和 queued 状态。独立服务器 dispatcher 每10秒检查待执行任务；CPU、内存、GPU 或数据盘暂时不足时保持原任务排队，资源满足后沿用原 ID 启动。dispatcher 不调用模型，Host 断网不影响队列和已启动作业。显存预留由 Controller 按可用容量分配；可选旧请求估计作为最低需求提示，Agent 无需读取服务器状态或计算可用显存。episode 执行器继续在分配内调整并发。Controller 的配置、诊断与运维属于部署者。

队列和输入保存于数据盘，取消 queued 作业后不会执行；活动管理索引只保留非终态作业，历史元数据保留。空闲一分钟后 dispatcher 退出，下次提交自动唤起，带进程身份验证和单实例锁。待执行队列可从同一数据目录恢复；服务器重启导致实际执行中断时不重放未知进度。启用前已运行的作业保持原 watcher。队列没有额外模型或 Agent 会话。

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

Agent 提交任务和科研参数，Controller 与 episode 执行器负责资源、生命周期和并发调整。`POST /jobs/wait` 一次等待最多 64 个作业；Host 复用 30 秒活动状态缓存、批量读取 watcher 元数据，终态缓存 5 分钟。接口返回简短终态或通知，等待超时由 Harness 内部续接，不唤醒模型。模型只在完成、中途停止或明确的人类通知时处理结果。研究代码错误由 Agent 分析；Controller 异常由操作者检查服务器记录。托管模式的部署说明和常规作业结果不展示服务器资源明细，具体请求见[执行说明](../agent-inputs/shared/execution.md)。

固定 Host 监督每 15 秒观察一次会话生命周期，仅状态变化追加监督事件；不驱动研究步骤或重启 Agent。作业 watcher 继续独立运行，不依赖 Agent 查询来推进训练或完成归档。
