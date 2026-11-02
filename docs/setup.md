# 环境配置指南

本项目使用独立 Python 环境管理 Agent runtime；仿真环境由 `a3_dual_arm_sim` 仓库管理。模型接入复用 mini-SWE-agent 现有的提供商适配器。

## 安装 Agent runtime

在项目根目录执行：

```bash
git submodule update --init --recursive
uv sync --python 3.11
uv pip check --python .venv/bin/python
uv run mini --help
```

`uv sync` 安装本项目及 `third_party/mini-swe-agent/` 中的 editable 依赖。依赖版本由 `pyproject.toml` 和 `uv.lock` 管理。

## 选择模型并启动 mini

按所选模型服务的要求配置认证信息，再设置以下两个环境变量：

| 变量 | 含义 |
| --- | --- |
| `MODEL_NAME` | mini 的模型适配器所接受的模型标识，按服务要求包含提供商前缀 |
| `MODEL_CLASS` | mini 的适配器名称，例如 `litellm`、`litellm_response`、`openrouter` 或 `portkey` |

这两个变量用于下面的命令示例。模型名称、适配器和认证方式需要与所选服务兼容。可用适配器见[模型注册入口](../third_party/mini-swe-agent/src/minisweagent/models/__init__.py)，接入说明见[上游模型指南](../third_party/mini-swe-agent/docs/models/quickstart.md)及对应服务的文档。凭据使用环境变量或服务支持的用户配置目录保存。

```bash
: "${MODEL_NAME:?请先设置模型标识}"
: "${MODEL_CLASS:?请先设置模型适配器}"
MSWEA_CONFIGURED=true uv run mini --model "$MODEL_NAME" \
  --model-class "$MODEL_CLASS" -c mini.yaml -c agent.step_limit=0
```

`MSWEA_CONFIGURED=true` 跳过首次配置向导，适用于已自行完成认证的环境。`step_limit=0` 表示不设总调用次数上限；成本限制使用 mini 的配置或 `--cost-limit` 参数设置。若所选服务缺少价格映射，可配置 `model.cost_tracking=ignore_errors` ；这时成本数据可能不可用。

当前命令启动第三方 mini 的交互入口，使用其 Bash 工具和逐步确认。无人交互运行使用 [RSI 使用教程](tutorial.md)，该入口使用独立容器工作区及Host 转发，关闭总步数、总费用与总时长限制，并使用无交互的 DefaultAgent。A3 专用工具、执行后端与自进化入口的实现状态见[开发路线](roadmap.md)。

## 准备仿真环境

按 `a3_dual_arm_sim` 的安装指南建立独立环境，并检查其 Python 版本、MuJoCo、训练可选依赖和渲染驱动要求。Agent runtime 与仿真环境各自安装依赖。

默认部署方案通过 SSH 连接远程 GPU 服务器。使用者配置 SSH 目标、项目目录、Python 环境、作业输出目录和可用 GPU；这些参数在后端实现后由部署配置传入。执行前分别验证 SSH 连通性、无相机仿真、相机渲染和所需的训练或推理组件。

具备兼容 GPU 和图形驱动的机器也可使用规划中的本地执行后端，直接运行相同作业入口。相机渲染是否可用取决于 MuJoCo、图形后端和设备驱动；训练与推理还需检查模型运行库的设备支持。

在仿真仓库激活其环境后，可按项目支持的命令检查：

```bash
a3-sim inspect
a3-sim smoke --steps 20
```

Linux 无显示器环境可按仿真项目要求设置 `MUJOCO_GL=egl`，并单独验证相机输出。执行 Agent 生成的候选代码前，按[冷启动规划](plans/2026-10-29-a3-harness-cold-start.md)准备容器与可写目录。宿主环境、容器环境和渲染链路分别验收。

远程镜像的构建与验证见[执行端部署](../runtime/remote/README.md)。镜像应包含与 A3 源码一起准备的公共 `docs/` 和 `examples/`。远程作业从 `/workspace` 执行，文档里的 `examples/...` 需改用 `/opt/a3/examples/...` 绝对路径，或采用文档提供的完整 Python 模块入口。部署检查应同时确认参考文件存在、评测入口的 `--help` 可运行；这项环境检查不代表 Policy 实验验收通过。

## 本地配置与运行产物

个人部署参数、认证信息和日志保存在版本控制之外。项目的 `artifacts/` 按 `rsi/`、`validation/` 和 `deployment/` 分别保存原始运行、验证与部署产物；目录职责见[项目结构](project-structure.md)。含个人资源或使用习惯的 Agent 指引文件应加入 `.gitignore`。提交示例配置时使用相对路径、清晰占位符和通用参数。


### 执行端 CPU、内存与临时文件

通过 `.env` 的 `A3_REMOTE_CPUS`、`A3_REMOTE_MEMORY`、`A3_REMOTE_PIDS_LIMIT` 配置单作业限额，默认分别为16 CPU、64 GiB、1024个进程与线程，不使用额外swap。`A3_REMOTE_TMP_ROOT` 应指向执行服务器的数据盘；未设置时使用该服务器的 `TMPDIR`。作业内使用 `/job-tmp` 及标准临时目录环境变量，`/tmp`保持只读。临时文件计入run存储额度并在退出后删除，checkpoint和长期产物应写入持久工作区或输出目录。配置在新建作业时生效，详情见[远程执行](../runtime/remote/README.md)。

## 可选研究网页网关（Tavily）

在启动 Host 的本机项目根目录 `.env` 配置，不在聊天中发送 key。当前 Mac 部署路径为 `/Users/dove/Projects/a3-research-agent/.env`；其他部署使用各自项目根目录或 `A3_RSI_ENV_FILE`。保留原有配置，仅加入或修改以下项：

```dotenv
TAVILY_API_KEY='<在本机填写自己的 key>'
A3_WEB_ENABLED='1'
```

仓库 `.env.example` 仅保留空 key，默认 `A3_WEB_ENABLED=0`。真实 key 只由 Host 读取，不复制到候选、执行服务器、Git、提示词或日志。用户自行管理 Tavily 账户额度与计费设置；Host 不自动购买、升级套餐或修改计费，若用户启用付费则可能产生费用。搜索固定使用 basic（每次提供商请求 1 credit，见 [Tavily 官方额度说明](https://docs.tavily.com/documentation/api-credits)）。搜索和 fetch 不设每 run 累计次数上限；仍保留并发、超时、响应大小与安全限制。各代、候选和同 run 恢复共用持久尝试计数，失败也计数，恢复不清零；这不是账户余额，Host 不轮询余额 API。

`A3_WEB_SEARCH_LIMIT` 和 `A3_WEB_FETCH_LIMIT` 已移除，旧 `.env` 中即使保留这些值也会被忽略；已配置的 key 无需更改。旧 run 的有限 limit 在新版 Host 读取时迁移为 null，保留累计 used 与审计历史。接口返回 `no_run_cap:true`；提供商 429 限流与 432/433 额度耗尽分别报告，前者有限退避，后者在额度或配置改变前不重复请求。网关不自动重试；可继续其他离线工作。

网页正文改由 Tavily Basic Extract 在提供商服务器读取，Host 只访问固定 `api.tavily.com`，不解析或连接目标网站，也不运行本地浏览器或 JavaScript。`/web/fetch` 仍接收 `url` 与 `max_chars`，返回提取正文而非原始 HTTP 页面，不是 PDF、模型或依赖下载器。Host 对目标 URL 做公开域名外形的词法检查，拒绝 IP、明显本地名称、凭据和不允许的端口；这不是目标域名的独立 DNS／重定向安全验证，实际抓取安全由提供商负责，不保证任意页面可读取。远程作业仍离线。

当前 Mac 可沿用已经运行的 Shadowrocket 本地 HTTP 代理：在 `/Users/dove/Projects/a3-research-agent/.env` **保留现有 key 和其他设置**，仅加入以下非敏感项：

```dotenv
A3_TAVILY_HTTP_PROXY='http://127.0.0.1:1082'
```

此选项仅影响 Tavily search／extract API，不更改系统代理、全局环境或其他网络链路。省略时直连 Tavily；只接受无凭据的数字回环 HTTP 代理地址。代理仅建立到固定 `api.tavily.com:443` 的 CONNECT 隧道，不携带 API 认证；隧道内仍端到端校验 Tavily TLS 证书及域名，API key 只在 TLS 内发送。Agent 不能选择代理、上游地址或自定义代理头；代理不可用时安全失败，不绕过或关闭 TLS 验证。配置仅在经批准启动／恢复 Host 时加载；本次修改本身不重启 E。

Basic Extract 按每 5 个成功提取 URL 消耗 1 credit，失败 URL 提取不收费（[官方额度说明](https://docs.tavily.com/documentation/api-credits)）。本地失败尝试仍计数；若提供商已成功处理而本地超时／响应丢失，不能据本地失败推断未扣费。没有 per-run 总次数上限、不自动开启付费的规则不变。

### 本地研究结果缓存

默认在同项目 Host 私有 `artifacts/web-cache` 中跨 run 复用成功搜索和提取正文；不缓存失败，不保存 key 或原始查询元数据，不交给候选直接挂载。默认配置可保留，也可在 Host `.env` 显式设置：

```dotenv
A3_WEB_CACHE_MAX_BYTES='500000000'
A3_WEB_SEARCH_TTL_SECONDS='86400'
A3_WEB_FETCH_TTL_SECONDS='604800'
```

容量为 500,000,000 字节（约 500 MB），搜索有效期 24 小时，正文 7 天；容量为 0 关闭全部缓存，单项 TTL 为 0 关闭该操作的缓存，三项配置均为非负整数。满后 LRU 淘汰较久未用的条目，不限制研究请求次数。容量按缓存文件计量，锁文件、目录与文件系统分配有少量额外开销，不保证物理磁盘占用精确等于配置值；可容纳页数取决于正文长度和序列化开销，不能准确保证。

未过期缓存命中不请求提供商、不新增该次提供商 credit；`retrieved_at` 保留原检索时间，新服务时间另记。显式 `refresh:true` 绕过缓存重新请求，可能消耗 credit；过期结果不会静默当新结果返回。Host 累计调用数与提供商请求尝试数分开（后者保守包含网络／提供商失败，不证明请求到达或扣费），均不是账户余额。Harness 仍须保存来源、时间、适用版本及谨慎解释，缓存不替代持久研究记忆或真实工具调用证据。

配置与代码在下一次经批准启动的新 run 使用；不会自动改动当前运行。沿用明确选定的第一阶段保留 H1，可先用 `--phase phase-one --task-profile web-extension --run-harness <第一阶段run目录>` 只补 E，再将新保留结果用于第二阶段。H1 自己实现、注册和测试检索工具，外层 Host 不代写工具，不重跑或重新认证 A–D。也可按第二阶段自身目标按需接入。项目根目录私有 `.env`／`A3_RSI_ENV_FILE` 加载方式不变，已有 key 不需重填或发送给助手。

`GET /web/status` 检查只读能力与尝试计数（不含账户剩余额度），不消耗付费搜索；不要为单纯配置检查发起真实搜索。E 验收另要求实际模型成功 search 与 fetch 正文，配置自报、status 和离线测试均不能替代。配置／key／网络／额度阻断时如实保存 blocked 并停止，不无限等待。接口见 [Host 执行说明](../agent-inputs/shared/execution.md#研究网页网关)，启动与运行干预的授权范围见 [操作者文档](operator-supervision.md)。
