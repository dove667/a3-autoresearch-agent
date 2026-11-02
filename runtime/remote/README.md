# 远程容器作业

Host控制器负责独立作业的执行和资源分配。模型服务、凭据、SSH 和作业归属映射留在容器外；实验策略和 Harness 演化由 Agent 完成。

## 部署

执行端需要 Linux、Python 3.10+、Docker Engine；GPU 作业还需要 NVIDIA Container Toolkit。已有 A3 项目应在 `.venv` 中提供可用的 Python 环境，并在 `.runtime/ffmpeg` 中提供兼容的 FFmpeg。

在服务器 checkout 本仓库，与本地使用相同的部署提交。controller 直接从 `runtime/remote/` 运行。
服务器仅运行 controller 时使用系统 Python 标准库，无需安装本地模型服务依赖或执行 `uv sync`。
需要运行 mini 的一端再初始化 submodule 并安装相应依赖。

在服务器仓库根目录设置部署变量，个人路径与镜像名称保存在 Git 忽略的配置中：

```bash
export JOB_ROOT="$PWD/artifacts/remote-runtime"
export CONTROLLER="$PWD/runtime/remote/remote_jobs.py"
export RUNTIME_IMAGE='<runtime-image>'
```

本地 `.env` 中的 `A3_REMOTE_CONTROLLER`、`A3_REMOTE_RUN_ROOT`、`A3_REMOTE_IMAGE` 分别填写
这三个变量在服务器上的值。`artifacts/remote-runtime/` 保存作业元数据、独立实验工作区和共享资源锁，
不进入 Git。镜像由服务器 Docker 管理，Git 更新源码时保留现有镜像与运行数据。

需要构建镜像时，指定已有 A3 checkout 和尚未存在的构建目录：

```bash
export BUILD_CONTEXT="$PWD/artifacts/deployment/builds/runtime"
python3 runtime/remote/prepare_image.py \
  --project "$A3_PROJECT" \
  --context "$BUILD_CONTEXT" \
  --image "$RUNTIME_IMAGE"
```

构建目录必须尚未存在，并与原项目位于同一文件系统，以便用硬链接准备依赖包。脚本复制项目资源、Python 环境和解释器，并在镜像内重建 editable 安装引用。项目凭据、Host Git 配置、数据集、实验输出和模型权重均不进入镜像。镜像带有仅保留当前 A3 HEAD 的独立浅仓库，供原始命令读取真实 commit；不复制 Host 的 remote、认证配置或 hooks。构建需要为 Python 环境和 Docker 构建层预留空间；基底为 `python:3.12-slim`，补充 EGL/OpenGL 系统库。

镜像同时包含该 A3 checkout 的 `README.md`、`docs/` 和 `examples/`。构建前检查工作流指南、动作契约与评测示例入口是否存在，缺失时拒绝构建。更新部署仓库里的准备脚本不会补齐旧镜像；已有部署需要构建新镜像、验证后切换 `A3_REMOTE_IMAGE`，并保留旧镜像供历史运行核对。

### 文档与命令位置

作业从 `/workspace` 开始执行，A3 基线及公共文档位于只读的 `/opt/a3`。A3 文档中的 `examples/...` 使用 A3 根目录作为相对路径；远程作业可使用完整模块名或绝对示例路径：

```bash
python -m a3_dual_arm_sim.workflows.benchmark_cookie_batch_cli --help
python /opt/a3/examples/benchmark_cookie_batch.py --help
```

两条命令应显示同一个评测入口的参数。`python -m a3` 和 `python -m a3_dual_arm_sim` 不是该评测入口。读取文档可使用 Python 标准库或系统已有命令；镜像不保证提供 `rg` 等额外开发工具。生成后的 Harness 自行选择怎样检索文档、保存和交接接口经验。

服务器部署 checkout 位于候选实验工作区之外，候选代码不能修改它。
同一执行端的所有控制器进程必须共享 `--root` 和资源配置，统一管理 GPU 卡数、显存预留与作业容量。容器获得自己的工作区、同身份输出目录及部署方配置的只读公共资产。

迁移已有部署前，停止提交新作业并等待已有作业及 watcher 结束；完整迁移资源根目录，保留其中的
`jobs/`、`experiments/` 和 `locks/`，再统一切换所有调用方的路径。迁移后检查历史产物仍可读取，
并验证提交、状态查询与归属拒绝。恢复运行前停用旧 controller 入口，避免出现两套独立资源锁。

## JSON 协议

每次调用从 stdin 读取一个 JSON 对象，向 stdout 输出一个 JSON 对象：

```bash
printf '{}' | python3 "$CONTROLLER" \
  --root "$JOB_ROOT" --image "$RUNTIME_IMAGE" health
```

| 操作 | 请求字段 | 返回内容 |
| --- | --- | --- |
| `health` | 无 | 状态、协议版本、镜像标识 |
| `submit` | `experiment_id`、`command`、`gpu`，可选 Host 注入的 `run_id`、`files` 与 `input_exports` | 作业标识、状态、分配的 GPU UUID |
| `status` | `experiment_id`、`job_id` | 状态、退出码、近期合并输出 |
| `cancel` | `experiment_id`、`job_id` | 终止或取消状态 |
| `artifact` | `experiment_id`、`job_id`、`path` | 路径、Base64 编码及内容 |
| `usage` | Host 提供的 `run_id`、`host_storage_bytes` | 更新本地占用并返回合计，不对 Agent 开放 |

受信任的 Gateway 注入 `experiment_id` 和整轮共享的 `run_id`；模型请求不能自行指定这两项。容器客户端提供容器内命令，例如 `["a3-sim", "smoke", "--steps", "20"]`，以及是否需要 GPU 的布尔值。挂载和 Docker 参数由 Host配置决定。`files` 将相对工作区路径映射到 UTF-8 内容；请求最多 20 MiB、128 个文件。

提交先返回 `created`，随后由独立 watcher 启动容器。状态包括 `created`、`running`、`completed`、`failed`、`cancelled`。资源不足时返回 `resource_busy`、空作业标识和重试间隔，该请求不启动容器。`reason=job_count_limit` 表示作业数量达到上限，`reason=no_eligible_gpu` 表示没有通过准入的 GPU；共享模式还返回 `gpu_allocation` 逐卡判断，避免将所有等待归因为显存不足。

未知作业、缺少归属和归属不匹配统一返回 `PermissionError` JSON。业务错误包含 `status: error`、错误类型和说明，输出后 CLI 退出码为 0；命令行参数错误沿用非零退出码。

通过 artifact 接口读取的产物必须是最多 32 MiB 的普通文件。接口拒绝绝对路径、路径越界和符号链接。状态输出截取最后 64 KiB，容器日志循环保存。

## 执行限制与 GPU 租约

每个作业挂载自己可写的 `/workspace`；有 run_id 时还挂载同一 worker 身份持久可写的 `/outputs`。固定 A3 基线位于 `/opt/a3`，整个容器根文件系统只读。容器关闭网络，移除 capabilities，启用 `no-new-privileges`，不提供 Docker socket、SSH 或模型凭据。

默认限制为 16 CPU、64 GiB 内存、禁用额外 swap、1024 个进程与线程、256 MiB 共享内存、2 个并发作业和 900 秒作业时限。部署方通过 `--cpus`、`--memory`、`--pids-limit`、`--max-jobs`、`--timeout` 调整（--max-jobs 0 不设作业数量上限，--timeout 0 不限时）；`--gpu-ids` 可限定 GPU 索引或 UUID。这些参数由 Host配置提供。

独占模式下，每个 GPU 作业持有单卡租约直到结束。共享模式使用 --share-gpus，允许同一卡运行多个作业；分配登记在共享调度锁内原子完成，持续有效的预留依据活动容器记录核算，不用整卡互斥锁阻止共享。

watcher 保存退出状态并终止超时作业。取消操作移除容器，覆盖已创建但尚未启动的情况；工作区和元数据保留供检查。调度器协调本 runtime 的作业，其他 Host工作负载沿用原有调度方式。

## 验证

```bash
python -m pytest tests/runtime/test_remote_jobs.py
python3 runtime/remote/verify_remote.py \
  --controller "$CONTROLLER" --root "$JOB_ROOT" \
  --image "$RUNTIME_IMAGE" --output "$VERIFICATION_REPORT"
```

集成检查提交临时实验，先检查公共参考文件及模块、示例两种评测入口，再验证工作区隔离、只读基线、归属拒绝、产物回传、CUDA、EGL 相机、资源限额，以及两个控制器竞争同一张 GPU。退出时取消本次仍在运行的作业。请在存在空闲 GPU 时运行；部署路径和验证记录保存在 ignored artifacts 中。

## 项目内的资产与运行存储

模型和数据放在部署方指定的项目数据目录中，通过 --models-root、--datasets-root 分别只读挂载到 /models、/datasets，不复制到镜像或每个作业。所有挂载路径由 Host 配置提供。公共数据由部署方提供；Agent 自己生成的数据写入自己的输出目录。

带 run_id 的新作业保存在 `artifacts/remote-runtime/runs/<run-id>/<owner>/jobs/<job-id>/`，同身份输出保存在相邻 outputs/。普通 job 之间可以复用同身份输出；不同身份无共享写目录。原有作业按其元数据保留原位置。

--run-storage-bytes 设置本地 run 文件与每轮新增远程数据、输出的合计额度，0 表示关闭限制。公共只读资产与通过符号链接引用的原始起点不计入；所有身份和作业的可写文件按逻辑字节数合计，符号链接不跟随。提交前检查已有内容与上传文件；Host 每五秒通过仅供部署方使用的 usage 操作报告本地占用，远程使用最近报告与远程文件之和。运行中由独立 watcher 每秒扫描，到额后正常停止自己的作业并保留证据。并发写入和停止期间允许短暂超额，因此不宣称文件系统级硬配额。额度不足返回 storage_limit，运行中超额以 failed 和明确原因保存。

本地 Host 通过 A3_REMOTE_MAX_JOBS、A3_REMOTE_JOB_TIMEOUT、A3_REMOTE_RUN_STORAGE_BYTES、A3_REMOTE_MODELS_ROOT、A3_REMOTE_DATASETS_ROOT 配置对应参数；health 返回实际限额与可用公共挂载。max-jobs 单独约束 CPU 与 GPU 作业数，设为 0 时不按数量限制。共享模式下，max-gpus 约束本项目同时使用的物理卡数；多个作业可以在同一卡并行，文件权限保持隔离。

## 显存共享调度

--share-gpus 开启同卡多作业；--max-gpus 限制整机本项目活动物理卡数。--gpu-memory-mb 是每个新作业缺省的显存峰值预留，Agent 可通过 gpu_memory_mb 提供实际预计值；--gpu-capacity-mb 限制每卡总预留（0 表示按设备容量减缓冲），--gpu-headroom-mb 保留显存缓冲，单位均为 MiB。它们由对应的 A3_REMOTE_SHARE_GPUS、A3_REMOTE_MAX_GPUS、A3_REMOTE_GPU_MEMORY_MB、A3_REMOTE_GPU_CAPACITY_MB、A3_REMOTE_GPU_HEADROOM_MB 提供。

调度器读取 nvidia-smi 的整卡实际显存，并用本项目 Docker 的进程归属区分已兑现预留和尚未加载的预留。外部占用计入整卡实际显存，本项目已加载的预留不重复计算。可用量取两项余量中的较小值：每卡预留上限减去本项目预留总额，以及物理显存减去缓冲、整卡实际占用和尚未兑现的预留。`--gpu-headroom-mb` 默认 2048，即 2 GiB；每卡预留上限与物理缓冲分别计算。没有空间时返回等待，已在运行的作业退出后名额自动释放。

共享模式允许与外部进程同卡，按上述显存余量准入，不设外部占用量或 GPU 利用率门槛。本项目同时使用的物理卡数仍受 --max-gpus 限制；外部进程不计入该卡数，控制器不调整外部进程。独占模式与独占租约仍保留原有约束。

每次共享 GPU 选择产生 `gpu_allocation`，包含请求显存、活动卡数、逐卡观测与判断。逐卡 `reason` 包括 `selected`、`eligible`、`device_not_allowed`、`exclusive_project_job`、`physical_device_limit`、`exclusive_lease_locked` 与 `insufficient_memory`；经过显存计算的条目同时包含 `capacity_mb`（本项目预留容量）、`physical_capacity_mb`（物理显存减缓冲）、`reserved_mb`、`pending_mb` 和 `available_mb`。返回给调用者的报告保存在 GPU 作业元数据中；选择或拒绝还追加到共享根目录 `scheduler-events.jsonl`，不记录命令、上传内容或凭据。诊断写入发生在创建 GPU 容器前，失败时不启动作业。报告对应本次提交时刻，不代表之后仍有相同资源；控制器不自动把 CPU 作业迁移到 GPU。

预留用于准入控制，不是 CUDA 硬显存配额。峰值估计应覆盖模型加载、训练与渲染需要；同卡并发会影响耗时，结果记录应保留共享条件。NVIDIA 指标字段见[官方 nvidia-smi 文档](https://docs.nvidia.com/deploy/nvidia-smi/index.html)。

## 离线训练环境

作业以 Host 数字 UID/GID 写入自己的文件，同时设置固定 USER/LOGNAME 与可写 HOME，供 PyTorch 等依赖解析用户名。编译缓存放在自己的输出目录。

--model-cache-root 可将项目专属缓存只读挂载为 /model-cache，并设置 HF_HOME、HF_HUB_CACHE 和 TORCH_HOME。缓存由人类准备，包含已批准模型与 tokenizer 的固定版本，不能挂载整个个人缓存目录。--preserve-models-path 将同一模型目录只读挂载到其原路径，供资产附属配置解析已有引用；不增加其他目录访问权限。HF Hub refs 文件直接保存 revision，不附加换行。

本地对应配置为 A3_REMOTE_MODEL_CACHE_ROOT、A3_REMOTE_PRESERVE_MODELS_PATH；A3_REMOTE_MEMORY 设置每个作业的系统内存上限。backbone、tokenizer 等预训练资源均由人类提前准备，作业关闭网络并强制离线加载，缺项按资源缺失处理。


## 跨身份只读二进制快照

`readonly_exports.py` 与 controller 一起部署，均只依赖 Python 标准库。Agent 的 HTTP 使用说明见 [Host 接口](../local/README.md#大文件只读交接)。以下 CLI 操作由受信任的 Gateway 调用，experiment_id 由 Host 注入，角色和接收人限制在 Gateway 校验。

| 操作 | 请求字段（另带 experiment_id） | 用途 |
| --- | --- | --- |
| export-create | job_id、location、path | 从自己的 outputs 或 workspace 选择文件/目录，异步封存 |
| export-status / export-manifest | export_id | 状态、摘要或逐文件清单 |
| exports-list | 无 | 当前身份获授权的快照 |
| exports-check | export_ids | 校验所有引用已 ready 且可读 |
| export-grant / export-revoke | export_id、recipient | 增加或撤回明确身份的只读权限 |
| export-delete | export_id | 原始所有者删除快照，不删除源文件 |

submit 的 input_exports 最多 64 个且不能重复；只挂载本身份获授权且 ready 的数据，每个引用对应 `/inputs/<export_id>/` 的只读目录。读取权不扩展为原 worker 目录的访问权；同一快照可在不同身份和后续 run 使用。源文件改变不影响已封存数据。

元数据和授权保存在 `readonly-exports/`，复制的数据、manifest 保存在 `runs/<来源run>/readonly-exports/<export_id>/`。独立 sealer 按块复制并计算摘要，原子发布 ready。复制失败清理未发布数据；状态查询发现 sealer 已退出会记录失败，不把它一直显示为 preparing。源文件应在封存期间保持稳定。符号链接和特殊文件被拒绝。

封存副本纳入来源 run 的存储监测；后续只读挂载不重复占用空间。删除和撤权与作业提交共享调度锁，活动消费者结束前拒绝回收。container 回收不删除快照；部署方清理历史 run 前，应核对 retained.json 中的 remote_exports 引用。授权和封存目录必须随共享资源根目录一同保留，才能继续恢复及跨轮引用。


## A3 源码版本记录

镜像提供 git，固定只读 A3 目录包含独立的 `.git`。采集和诊断入口可直接执行 `git -C /opt/a3 rev-parse HEAD`，无需 Agent 补装依赖或伪造版本号。数字 UID 作业通过系统级 safe.directory 允许读取该目录；根文件系统仍只读，作业不能改动基线版本库。

prepare_image.py 从部署方提供的 A3 checkout 导出真实 HEAD 的浅对象与索引，并记录 `/opt/a3/source-revision.json`：commit、源工作区是否有改动，以及实际准备进镜像的源码/资源文件 SHA256。工作区有改动时，commit 与文件摘要一起用于定位有效源码；不能只凭 HEAD 声称文件与提交逐字一致。安装环境和运行时依赖不纳入该源码清单。

已有镜像不会随部署脚本更新。更新已有部署时保留旧标签，在新标签中补齐 git 与来源信息；复用旧镜像层时先逐文件核对原镜像和来源 checkout，匹配后才附加该来源的 Git 信息。用实际数字 UID、只读根文件系统和无网络的作业验证版本读取与原采集程序的元数据代码，再切换部署配置。历史运行输入和结果保留旧镜像信息，新作业使用当前配置。


## 临时文件与数据盘

`--tmp-root` 指定数据盘上的临时根目录；省略时采用执行服务器的 `TMPDIR`，两者均未设置则使用运行根目录下的 `temporary/`。Host 为每个作业创建独立目录并仅将该目录挂载为 `/job-tmp`；`TMPDIR`、`TMP`、`TEMP`、`HOME` 和可写缓存指向这里。Docker 根文件系统只读，`/tmp` 不提供可写挂载，硬编码该路径的程序应改用标准临时目录接口。临时文件计入所属 run 的存储额度，作业退出后回收；需恢复的 checkpoint 和产物写入 `/outputs` 或 `/workspace`。不同作业不共享临时目录。

Host 环境变量 `A3_REMOTE_CPUS`、`A3_REMOTE_MEMORY`、`A3_REMOTE_PIDS_LIMIT`、`A3_REMOTE_TMP_ROOT` 对应这些参数。新配置用于新建作业；已运行作业的挂载与环境保持原样。Docker daemon 自身的元数据和有界日志仍位于其配置的数据目录，若要迁移该目录，应由服务器管理员另行安排。

### NVIDIA EGL discovery

GPU jobs mount the controller's `nvidia_egl_vendor.json` read-only and set
`__EGL_VENDOR_LIBRARY_FILENAMES` to that descriptor. This selects the NVIDIA EGL
implementation already injected by the NVIDIA container runtime, even when an
image only contains Mesa's vendor descriptor. Deploy this file alongside
`remote_jobs.py`. CPU jobs retain their image's default discovery behavior.
This does not install drivers, change GPU allocation, or relax container isolation.
Verify `GL_VENDOR` and `GL_RENDERER` inside a GPU job; successful context creation
alone can also indicate software rendering.
