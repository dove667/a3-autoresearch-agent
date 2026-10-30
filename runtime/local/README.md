# 宿主提供的执行与验收入口

部署方运行 gateway、容器引擎、模型认证与远程资源调度。候选 Harness 在独立容器内运行，
通过 HTTP 请求这些固定能力。工具名称、研究逻辑、反思方法和版本选择由 Agent 设计。
目标和验收以本次只读的 `goal.md`、`acceptance.md` 为准。

## 请求与权限

gateway 地址和本身份的令牌由 `A3_GATEWAY_URL`、`A3_GATEWAY_TOKEN` 环境变量提供。
请求携带 `Authorization: Bearer <本身份令牌>`，不要把令牌打印到轨迹或存入报告。
模型使用 Responses 接口 `/v1/responses`，别名 `openai/sandbox-model`，适配器 `litellm_response`。
`OPENAI_API_BASE` 与 `OPENAI_API_KEY` 已配置为本轮转发入口；实际模型固定在宿主。
调用费用未知时保持未知，token 用量从响应读取。

## 独立候选执行

`POST /sessions` 接受 `{"command": ["python", "entry.py"], "files": {"entry.py": "..."}}`，
返回 `session_id`。上传需要运行的候选文件；128 个 UTF-8 文件合计不超过 20 MiB。
命令参数使用数组。不能指定 Docker 参数、挂载、宿主命令、身份或 GPU。

宿主创建同级容器，工作目录为 `/workspace`，上传文件写入该目录。
其身份由宿主独立分配并注入，调用方拿不到子身份令牌。候选容器不能读取构建者或其他候选的目录、令牌和模型认证。
它只能访问自己的可写工作区、只读 `/run/input` 和只读公共 A3 源码 `/opt/a3`。
无需在容器内创建 namespace、安装 Docker 或运行嵌套容器。
固定限制为 2 CPU、2 GiB 内存、256 个进程、512 MiB 临时目录，同时最多两个活动候选容器。
本地镜像提供 mini 和模型客户端；A3 仿真依赖在远程镜像中。

`GET /sessions/{session_id}` 返回状态、退出码和近期输出，Docker 完成状态为 `exited`。
`GET /sessions/{session_id}/artifacts?path=relative_path` 返回最多 32 MiB 文件的 Base64 内容。
`DELETE /sessions/{session_id}` 回收该候选及后继候选、相关远程作业和隔离 fixture，
保存控制台输出、移除容器并撤销这些候选身份，工作区保留。
如果部分资源暂时无法回收，返回错误；父调用方仍可用原 session ID 重试清理。
只有创建该候选的身份可以管理它，候选不能据此查看或管理父身份。
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

从需要验收的研究 Agent 身份调用 `POST /fixtures/isolation`，宿主创建一个真实的异身份 CPU 作业，
返回 `fixture_id`、`peer_job_id`、`peer_artifact_path` 与哨兵 SHA-256。其身份和认证留在宿主。
该作业最多等待 600 秒，并占用一个远程并发名额，不占 GPU。

研究 Agent 使用自己的身份尝试查询、取消、读取这个异身份作业，实际请求应被拒绝。
伪造 `experiment_id` 参数也应被拒绝。用自己的合法作业证明正常访问，
并在真实作业容器中检查无法访问异实验目录、共享基线不可写。
这组资源用于验收，不授权操作其他真实实验。

随后 `GET /fixtures/isolation/{fixture_id}` 由宿主核对哨兵未变及异身份作业仍在，返回真实结果。
记录请求、拒绝、核验和正常操作证据后，调用 `DELETE /fixtures/isolation/{fixture_id}` 清理。
每个身份同时最多一个 fixture，可清理后重新创建。启动器退出时也会清理本轮 fixture。
接口成功或宿主预检通过不能代替研究 Agent 自己执行 A、B、C 的验收证据。

启动器退出时先停止接受新候选、模型请求、作业和 fixture，再逐项清理本轮资源。
某项失败仍会继续处理其他资源，并保存 `cleanup-errors.json` 供复核。
