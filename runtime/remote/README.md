# 远程容器作业

宿主控制器负责独立作业的执行和资源分配。模型服务、凭据、SSH 和作业归属映射留在容器外；实验策略和 Harness 演化由 Agent 完成。

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
export BUILD_CONTEXT="$PWD/artifacts/image-build/runtime"
python3 runtime/remote/prepare_image.py \
  --project "$A3_PROJECT" \
  --context "$BUILD_CONTEXT" \
  --image "$RUNTIME_IMAGE"
```

构建目录必须尚未存在，并与原项目位于同一文件系统，以便用硬链接准备依赖包。脚本复制项目资源、Python 环境和解释器，并在镜像内重建 editable 安装引用。项目凭据、Git 元数据、数据集、实验输出和模型权重均不进入镜像。构建需要为 Python 环境和 Docker 构建层预留空间；基底为 `python:3.12-slim`，补充 EGL/OpenGL 系统库。

服务器部署 checkout 位于候选实验工作区之外，候选代码不能修改它。
同一执行端的所有控制器进程必须共享 `--root` 和资源配置，统一管理作业数和 GPU 锁。容器只获得自己的工作区。

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
| `submit` | `experiment_id`、`command`、`gpu`，可选 `files` | 作业标识、状态、分配的 GPU UUID |
| `status` | `experiment_id`、`job_id` | 状态、退出码、近期合并输出 |
| `cancel` | `experiment_id`、`job_id` | 终止或取消状态 |
| `artifact` | `experiment_id`、`job_id`、`path` | 路径、Base64 编码及内容 |

受信任的 gateway 注入 `experiment_id`。容器客户端提供容器内命令，例如 `["a3-sim", "smoke", "--steps", "20"]`，以及是否需要 GPU 的布尔值。挂载和 Docker 参数由宿主配置决定。`files` 将相对工作区路径映射到 UTF-8 内容；请求最多 20 MiB、128 个文件。

提交先返回 `created`，随后由独立 watcher 启动容器。状态包括 `created`、`running`、`completed`、`failed`、`cancelled`。资源不足时返回 `resource_busy`、空作业标识和重试间隔，该请求不启动容器。

未知作业、缺少归属和归属不匹配统一返回 `PermissionError` JSON。业务错误包含 `status: error`、错误类型和说明，输出后 CLI 退出码为 0；命令行参数错误沿用非零退出码。

产物必须是最多 32 MiB 的普通文件。接口拒绝绝对路径、路径越界和符号链接。状态输出截取最后 64 KiB，容器日志循环保存。

## 执行限制与 GPU 租约

每个作业只挂载一个可写的 `/workspace`。固定 A3 基线位于 `/opt/a3`，整个容器根文件系统只读。容器关闭网络，移除 capabilities，启用 `no-new-privileges`，不提供 Docker socket、SSH 或模型凭据。

默认限制为 2 CPU、8 GiB 内存与交换空间总额、256 个进程、256 MiB 共享内存、512 MiB 临时空间、2 个并发作业和 900 秒作业时限。部署方通过 `--cpus`、`--memory`、`--pids-limit`、`--max-jobs`、`--timeout` 调整；`--gpu-ids` 可限定 GPU 索引或 UUID。这些参数由宿主配置提供。

每个 GPU 作业最多获得一张卡。分配器跳过显存占用超过 128 MiB 或利用率超过 5% 的设备；分配后由独立 watcher 继承并持有宿主 `flock`，直至容器结束。持久化容器租约使 watcher 意外退出后仍能识别正在运行的作业，避免重复分配。共享调度锁串行完成容量检查和容器创建。

watcher 保存退出状态并终止超时作业。取消操作移除容器，覆盖已创建但尚未启动的情况；工作区和元数据保留供检查。调度器协调本 runtime 的作业，其他宿主工作负载沿用原有调度方式。

## 验证

```bash
python -m pytest runtime/remote/test_remote_jobs.py
python3 runtime/remote/verify_remote.py \
  --controller "$CONTROLLER" --root "$JOB_ROOT" \
  --image "$RUNTIME_IMAGE" --output "$VERIFICATION_REPORT"
```

集成检查提交临时实验，验证工作区隔离、只读基线、归属拒绝、产物回传、CUDA、EGL 相机、资源限额，以及两个控制器竞争同一张 GPU。退出时取消本次仍在运行的作业。请在存在空闲 GPU 时运行；部署路径和验证记录保存在 ignored artifacts 中。
