# 环境配置指南

本地 Python 环境运行 Host 和研究 Harness；A3 仿真、渲染、采集与训练环境独立部署在执行端。

## 安装

```bash
git submodule update --init --recursive
uv sync --python 3.11
uv pip check --python .venv/bin/python
cp .env.example .env
```

依赖由 `pyproject.toml` 和 `uv.lock` 固定。第三方 mini-SWE-agent 可独立通过 `uv run mini` 使用，其配置见[上游指南](../../third_party/mini-swe-agent/docs/models/quickstart.md)。常规研究入口直接运行项目已发布的 Harness。

## 模型服务

Host 使用 LiteLLM Responses 转发模型请求。`MODEL_NAME` 指定提供商模型标识，认证使用该提供商支持的 Host 环境变量。自定义 Responses 服务可以配置：

```dotenv
MODEL_NAME='provider/model-id'
A3_UPSTREAM_RESPONSES_BASE_URL='https://provider.example/v1'
A3_UPSTREAM_RESPONSES_MODEL='model-id'
A3_UPSTREAM_RESPONSES_API_KEY=''
A3_UPSTREAM_RESPONSES_READ_TIMEOUT_SECONDS='300'
```

Host 在基础地址后追加 `/responses`。自定义上游覆盖主模型、视觉辅助与摘要的实际模型，各角色仍可以指定提供商支持的推理参数。密钥只保存在 Git 忽略的 `.env` 或 Host 认证环境中。

`A3_WORKER_MODEL_NAME` 和 `A3_SUMMARY_MODEL_NAME` 配置固定辅助与摘要路由，省略时使用主模型。实际上下文窗口必须按所用服务填写：

```dotenv
A3_MODEL_CONTEXT_TOKENS='262144'
A3_WORKER_CONTEXT_TOKENS='262144'
A3_SUMMARY_CONTEXT_TOKENS='524288'
A3_CONTEXT_TRIGGER_TOKENS='196608'
A3_CONTEXT_TARGET_TOKENS='32768'
```

窗口、触发值和目标值彼此独立。压缩目标覆盖下一轮完整输入；摘要只使用扣除固定指令、工具、运行状态和最近完整回合后的空间。完整机制见[上下文管理](../reference/context-and-memory.md)。模型、推理参数、路由与预算在普通恢复时保持一致，显式维护发布可以更新并归档前值。

## 本地进程与研究目录

本地不需要 Docker。Host 在项目 Python 环境中创建一个原生研究进程，固定发布源码与可写研究目录分开保存。Host 在 `runtime_paths` 给出工作区、只读输入、A3 参考源码和会话状态路径；`A3_STATE_ROOT` 指向当前 run 的 `conversation/`，`A3_PLAN_FILE` 指向项目根 `plan.json`。Agent 使用文件工具准备研究，通过远程 jobs 执行代码，工具集不提供本地命令执行。

A3 仓库、SSH 目标和远程路径由 `.env` 指定。模型执行与机器状态保存到 `trajectories/<开始时间>/conversation/`，研究文件保存在唯一的 `workspace/`；根 `skills/`、`memory/entries.jsonl` 和 `plan.json` 分别通过专用工具管理。研究计划跨运行共用，trajectory 的 `plan-snapshots/` 保存各运行期间的历史快照。调试日志保存到 `runs/<开始时间>/`，维护验证原始输出集中到 `runs/checks/`。Host 状态与可重建源码缓存位于 `.runtime/`，网页内容缓存位于 `web-cache/`。运行数据目录和 `.env` 保持 Git 忽略。

启动时 Host 加载项目根 `.env`；`A3_RSI_ENV_FILE` 可选择另一份完整配置，文件值覆盖同名进程环境变量。现有部署只修改所需项，保留凭据与私人路径。

## 执行端

执行端提供 Linux、可用的 A3 Python 环境和所需 GPU／渲染驱动。`process` 后端直接使用配置的 Python 与真实数据目录；Docker 后端需要额外准备远程镜像。分别验证 SSH、无相机仿真、相机渲染及实际训练依赖。

所有 Controller 共用资源根目录与资源配置。托管队列管理等待、显存预算和作业生命周期；具体配置见[进程后端](../reference/remote-process.md)和[远程容器部署](../reference/remote-docker.md)。

```bash
bash start-autoresearch.sh --check
```

此检查验证配置、A3 路径和 Controller 健康响应。真实模型调用、训练和科学评估由相应验证或研究任务完成。

## 网页研究

需要联网研究时设置 `A3_WEB_ENABLED=1` 和 `TAVILY_API_KEY`。Host 提供搜索、提取、缓存和请求计数；凭据不交给研究进程。网页代理使用独立的 `A3_TAVILY_HTTP_PROXY` 配置，完整字段见 `.env.example`。

首次启动、同一 run 恢复、原地发布与夜间窗口见[使用教程](usage.md)。长期研究目标和行为原则由 `prompt/SOUL.md` 提供，Host 按版本装载三个 prompt，根技能库独立管理版本和修改历史。
