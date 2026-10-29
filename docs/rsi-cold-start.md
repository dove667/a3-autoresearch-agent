# 容器化冷启动

冷启动使用独立容器工作区。宿主保留模型认证、SSH 和作业分配权限，通过固定 gateway 转发模型请求及远程作业。研究与自进化逻辑由容器中的 Agent 产生。

## 准备与启动

安装依赖及 submodule 后，构建本地镜像：

```bash
bash scripts/build-bootstrap-image.sh
```

将 `.env.example` 复制为 Git 忽略的 `.env`，配置模型、A3 工作区、本地镜像以及远程 controller 与镜像。远程部署工具位于 `runtime/remote/`。当前启动路径面向 macOS Docker Desktop；本地 GPU 和其他桌面平台尚未适配。

```bash
bash scripts/start-rsi.sh --check
bash scripts/start-rsi.sh
```

`--check` 检查本地镜像与远程 controller 可达性。真实模型、GPU 和渲染需分别验证。正式运行无需交互，不设总 step、代数、总费用或总时长上限；单条命令和单个作业有独立的超时及资源限制。网络、配额或上下文错误仍可能中断，恢复与上下文交接纳入研究能力验收。

## 工作区与权限

启动器复制 A3 和 Harness 的 Git 工作区，包括可提交的当前修改，排除 Git 忽略的个人配置与凭据；第三方源码拥有独立 Git 历史。容器从 A3 工作目录开始，只挂载本轮副本和只读目标输入。宿主初始 mini 运行器保持固定，候选代码只能在容器内加载。

模型转发固定使用宿主配置的模型服务，当前 gateway 提供 Responses 接口。容器使用本轮专属访问令牌，不持有上游认证或 SSH 凭据。正式研究入口的工具形态由 Agent 根据目标产生。

作业 controller 在远程宿主运行，为每个作业创建独立容器及可写目录；A3 基线在镜像内，容器根文件系统只读、网络关闭，并限制 CPU、内存和进程数量。GPU 由 controller 分配；同一 controller 根目录下的各进程共享锁，避免给本项目的多个运行实例重复分配同一张卡。

默认资源策略面向正常实验之间的误操作隔离。宿主、镜像、挂载、controller 根目录和资源参数由部署方控制，不交给候选代码修改。目标与验收见 [goal.md](../evals/goal.md) 和 [acceptance.md](../evals/acceptance.md)。

## 记录与复核

每次启动在 Git 忽略的 `artifacts/rsi/` 下创建独立目录。`inputs/` 保存冻结目标、验收、配置和容器可用入口；`workspace/` 保存候选仓库与子会话产物；`startup.json` 记录起点及输入摘要；`trajectory.json` 保存初始 mini 轨迹；`model-usage.jsonl` 保存经 gateway 转发的调用用量。后继执行保存独立轨迹。

`artifacts/rsi/latest-run.txt` 指向最近运行目录。启动器退出时关闭本地容器与转发服务；远程已提交作业由 controller 管理，Agent 可查询、取回产物或取消自己的作业。个人部署选择与验证记录维护在本地 AGENTS.md 和 Git 忽略的产物中。
