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

`MODEL_NAME` 配置 Main Agent。`A3_WORKER_MODEL_NAME` 配置 Visual Assistant Agent 和 Deep Research Agent，`A3_SUMMARY_MODEL_NAME` 配置 Compressor Agent，省略时使用主模型。实际上下文窗口必须按所用服务填写：

```dotenv
A3_MODEL_CONTEXT_TOKENS='262144'
A3_WORKER_CONTEXT_TOKENS='262144'
A3_SUMMARY_CONTEXT_TOKENS='524288'
A3_CONTEXT_TRIGGER_TOKENS='196608'
A3_CONTEXT_TARGET_TOKENS='32768'
```

窗口、触发值和目标值彼此独立。压缩目标覆盖下一轮完整输入；摘要只使用扣除固定指令、工具、运行状态和最近完整回合后的空间。完整机制见[上下文管理](../reference/context-and-memory.md)。

使用较弱的主模型时，把摘要路由固定到更强的模型，并把触发点放低；摘要质量不再跟随主模型，剩余不测由程序降级交接兜底：

```dotenv
A3_SUMMARY_MODEL_NAME='provider/strong-summary-model'
A3_MODEL_CONTEXT_TOKENS='1048576'
A3_SUMMARY_CONTEXT_TOKENS='1048576'
A3_CONTEXT_TRIGGER_TOKENS='131072'
A3_CONTEXT_TARGET_TOKENS='32768'
```

评估一个候选模型时，先看 `conversation/research-ledger.jsonl` 的作业与压缩事件，以及 trajectory 中 `model_drift`、`tool_choice_unsupported` 和 `summary_mode="degraded"` 的出现频率，再决定是否调整提示词或工具面。模型、推理参数、路由与预算在普通恢复时保持一致，显式维护发布可以更新并归档前值。

## 本地进程与研究目录

Host 在项目 Python 环境中创建一个原生研究进程，固定发布源码与可写研究目录分开保存。Host 在 `runtime_paths` 给出工作区、只读输入、A3 参考源码和会话状态路径；`A3_STATE_ROOT` 指向当前 run 的 `conversation/`，`A3_PLAN_FILE` 指向 `.runtime/research-plan/current.json`。Agent 使用文件工具准备研究，通过远程 jobs 执行代码，工具集不提供本地命令执行。

A3 仓库、SSH 目标和远程路径由 `.env` 指定。模型执行与机器状态保存到 `trajectories/<开始时间>/conversation/`，研究文件保存在唯一的 `workspace/`；根 `skills/`、`memory/entries.jsonl` 和 `.runtime/research-plan/current.json` 分别通过专用工具管理。研究计划跨运行共用，trajectory 的 `plan-snapshots/` 保存各运行期间的历史快照。调试日志保存到 `runs/<开始时间>/`，维护验证原始输出集中到 `runs/checks/`。Host 状态与可重建源码缓存位于 `.runtime/`，网页内容缓存位于 `web-cache/`。运行数据目录和 `.env` 保持 Git 忽略。

启动时 Host 加载项目根 `.env`；`A3_RSI_ENV_FILE` 可选择另一份完整配置，文件值覆盖同名进程环境变量。现有部署只修改所需项，保留凭据与私人路径。

## 执行端

执行端提供 Linux、可用的 A3 Python 环境和所需 GPU／渲染驱动。Controller 使用配置的 Python 和数据盘目录直接启动作业。分别验证 SSH、无相机仿真、相机渲染及实际训练依赖。

所有 Controller 共用资源根目录与资源配置。托管队列管理等待、显存预算和作业生命周期；具体配置见[远程进程作业](../reference/remote-process.md)。

```bash
bash start-autoresearch.sh --check
```

此检查验证配置、A3 路径和 Controller 健康响应。真实模型调用、训练和科学评估由相应验证或研究任务完成。

## 网页研究

需要联网研究时设置 `A3_WEB_ENABLED=1` 和 `TAVILY_API_KEY`。Host 提供搜索、提取、缓存和请求计数；凭据不交给研究进程。网页代理使用独立的 `A3_TAVILY_HTTP_PROXY` 配置，完整字段见 `.env.example`。

Agent 用 `web_search` 分别检索官方源码与论文，用 `web_fetch` 读公开原文。`web_fetch(url=..., query=...)` 返回与问题相关的正文片段，适用于很长的训练文档或论文 HTML。论文优先使用 arXiv HTML、OpenReview 或会议出版社的公开页面。当前网关接受文本网页，PDF 文件不经过此入口。

Deep Research Agent 使用 Tavily 查公开代码和网页。要启用 alphaXiv 的论文检索和 PDF 原文读取，在本机 Host 的私有 `.env` 配置 `A3_ALPHAXIV_TOKEN`。Host 用它直接连接 alphaXiv MCP。部署未配置 alphaXiv 时，论文可经 Tavily 的公开 HTML 获取。

首次启动、同一 run 恢复、原地发布与夜间窗口见[使用教程](usage.md)。长期研究目标和行为原则由 `prompt/SOUL.md` 提供，Host 按版本装载三个 prompt，根技能库独立管理版本和修改历史。


## 本地知识检索服务

安装带 Metal 支持的 `llama-server`（其他平台可使用相应 CPU 构建），并使其位于 PATH；可用 `A3_EMBEDDING_SERVER` 指定可执行文件。准备模型：

```bash
uv run python -m a3_research_agent.host.knowledge_service.model --project-root .
```

默认模型为官方 [Qwen3-Embedding-0.6B Q8 GGUF](https://modelscope.cn/models/Qwen/Qwen3-Embedding-0.6B-GGUF)，下载后校验 SHA256 并保存在 `model-cache/`。常规启动器在实际研究启动时启动本地知识检索服务，停止时关闭；`--check` 不启动推理服务。实验期间使用缓存模型，索引在 `memory/` 持久保存。
