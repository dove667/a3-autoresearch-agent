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
A3_WEB_SEARCH_LIMIT='100'
A3_WEB_FETCH_LIMIT='200'
```

仓库 `.env.example` 仅保留空 key，默认 `A3_WEB_ENABLED=0`。真实 key 只由 Host 读取，不复制到候选、执行服务器、Git、提示词或日志。用户自行管理 Tavily 免费账户，保持提供商付费计费与自动付费升级关闭；Host 不自动升级套餐。搜索固定使用 basic（每次 1 credit，见 [Tavily 官方额度说明](https://docs.tavily.com/documentation/api-credits)）；本地每 run 的搜索/fetch 上限是额外保护，不能保证账户全局免费余额（其他 run、程序或使用者也可能耗费额度）。各代、各候选和同 run 恢复共用持久计数，不能换代重置。

网页正文由 Host 直接抓取，仅支持受约束的公开 HTTP(S) HTML/纯文本；不执行 JavaScript，也不用于 PDF、模型或依赖下载。DNS 校验及固定连接地址/TLS 校验约束此网关，不代表所有公网地址均可达，也不代表候选容器已部署通用出站防火墙。远程作业的离线设置不变。

配置与代码在下一次经批准启动的新 run 使用；本次配置不会自动改动当前运行。新 run 沿用明确选定的第一阶段保留 H1，并由第二阶段 H1 自己实现、注册和测试检索工具，不重跑第一阶段。检查 `GET /web/status` 只读能力与额度，不消耗付费搜索；不要为配置检查发起真实搜索。接口见 [Host 执行说明](../runtime/local/README.md#研究网页网关)，运行干预的逐次批准见 [操作者文档](operator-supervision.md)。
