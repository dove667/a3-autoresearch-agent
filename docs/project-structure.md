# 目录职责与产物管理

固定实现进入 Git；研究工作区、运行证据、部署配置和报告留在本地。

```text
.
├── src/a3_research_agent/
│   ├── harness/           模型循环、工具、上下文、记忆与 skills
│   ├── host/              发布、Gateway、进程、输入与运行记录
│   ├── execution/         Controller、作业、GPU 调度和远程镜像
│   ├── packaging/         发布源码载荷清单
│   ├── workspace/         路径校验
│   └── cli/               启动和操作者命令
├── agent-inputs/          当前通用角色、接口与工作区指引
├── archive/               历史阶段提示词
├── skills/                项目研究方法包
├── tests/                 固定实现的回归与打包检查
├── scripts/               薄命令入口
├── docs/                  安装、使用、架构与接口说明
├── third_party/           固定版本的第三方 submodule
├── workspace/             研究代码、上下文与知识，Git 忽略
├── artifacts/             运行、验证和部署记录，Git 忽略
├── reports/               可选本地报告，Git 忽略
├── .env.example           公共部署模板
└── .env                   本机配置，Git 忽略
```

## 源码边界

`host/launch.py` 装配并启动服务，`host/gateway/` 按模型、作业、会话、网络与操作者控制分组，所有路由共用同一身份注册表、锁和 checkpoint。

`host/sessions/processes.py` 只处理本地原生进程生命周期；`host/sessions/workspace.py` 提供当前工作区和受限文件操作。`host/runs/` 管理运行状态、checkpoint 配对、恢复与保留结果；`host/releases.py` 处理暂停期间的原地发布事务。

`execution/controller/` 使用标准库实现作业入口，`execution/scheduling/` 负责资源准入，`execution/images/` 保存远程容器构建资源。远程 Controller 从 `src/a3_research_agent/execution/controller/__main__.py` 或安装后的命令运行。所有 Python 导入使用 `a3_research_agent.*`。

Harness 通过 Gateway 请求模型、计算和外部网络。工具参数校验、文件预处理和结果展示归属 `harness/`；身份、认证和转发归属 Host；计算资源与作业状态归属 Controller。

## 研究与运行目录

```text
workspace/<run-id>/<session-id>/
├── research/              Agent 编写的研究脚本与分析
├── skills/                工作区 skills
├── memory.jsonl           长期经验及来源
├── checkpoint.json        模型上下文与恢复状态
└── …                      工具 journal、轨迹、计划与研究文件

artifacts/rsi/<run-id>/
├── run.json               任务输入来源与启动选择
├── inputs/                当前部署方输入
├── input-revisions/       输入的完整历史版本
├── input-events.jsonl     输入版本事件
├── sessions/<session-id>/ 原生会话记录、控制台及 workspace 链接
├── .host/                 私有状态、锁与发布事务
├── fixtures/              隔离检查记录
├── retained-knowledge/    显式保留的不可变研究知识快照
├── retained.json          发布源码与知识选择的结果
├── supervision.json       Host 监督状态
├── model-usage.jsonl       模型调用记录
├── session-events.jsonl    会话生命周期
└── gateway-events.jsonl    请求、错误与耗时
```

运行目录按 Host 本地时间命名为 `run-YYYYMMDD-HHMMSS-ffffff`，重名时原子追加序号。`artifacts/rsi/latest-run.txt` 只帮助定位最近运行。恢复和新任务均显式指定来源。

研究工作区独立于 Harness 源码缓存。`artifacts/published-harness/v2/<commit>/` 保存固定发布源码；原地升级继续使用同一研究目录。升级边界保存在 `.host/release-upgrades/`，事件保存在 `.host/release-events.jsonl`。私有状态、认证和锁不作为研究产物分享。

## 其他产物

| 路径 | 用途 |
| --- | --- |
| `artifacts/validation/` | 测试、部署预检、恢复和安装验证 |
| `artifacts/deployment/` | 构建材料、迁移清单与部署修复 |
| `artifacts/night-window/` | 夜间调度与发布选择 |
| `artifacts/remote-runtime/` | 执行端作业、租约、显存预算与导出授权 |
| `artifacts/project-job-index.json` | 项目历史远程作业索引 |
| `artifacts/models/`、`artifacts/datasets/` | 可选的公共权重和数据 |
| `reports/` | 本地展示报告及其构建结果 |

所有 Controller 必须共用配置的执行端资源根目录；切换源码版本不创建另一套 GPU 锁。远程数据和导出快照留在执行端，`retained.json` 只保存选定引用。

历史档案保留用于复核。旧 builder、本地容器和旧生成 Harness 格式不进入当前启动路径。需要继续的研究由维护者离线整理为当前原生 schema，并保留整理前的记录和校验清单。

部署参数从 `.env.example` 复制配置，读取规则见[环境配置](setup.md)。Agent 输入版本与文件清单见[输入管理](agent-inputs.md)。报告目录保持 Git 忽略，公开文档只引用仓库内可获得的说明。
