# 目录职责与产物管理

项目将固定基础设施、SWE Agent 生成的候选、原始运行档案与展示报告分别维护。先看项目能力与启动方式可读根目录 README；查看冷启动产生的代码可从 `harnesses/` 进入。

```text
.
├── src/a3_research_agent/   Python 包与后续稳定接口
├── runtime/
│   ├── local/             本地启动器、Gateway、候选执行与恢复
│   └── remote/            远程作业 Controller、镜像构建与验证入口
├── harnesses/              SWE Agent 生成的候选源码快照与来源
├── configs/                通用启动配置
├── evals/                  当前目标与验收标准
├── tests/runtime/          本地和远程基础设施的回归测试
├── scripts/                薄启动入口
├── third_party/            固定版本的 mini-SWE-agent submodule
├── reports/                Data 网页报告工程与审阅后的展示数据
├── docs/                   环境指南、架构、路线与历史设计
└── artifacts/              本地运行档案、验证记录和部署材料
```

## 源码与职责

| 目录 | 负责什么 | Git 管理 |
| --- | --- | --- |
| `src/a3_research_agent/` | 项目 Python 包；当前稳定部署实现仍位于 `runtime/` | 是 |
| `runtime/local/` | Host 固定的模型转发、身份与权限、候选生命周期、冷启动和恢复 | 是 |
| `runtime/remote/` | 按归属运行远程 Docker 作业、分配资源、构建执行镜像 | 是 |
| `harnesses/` | 保存 SWE Agent 已生成的完整候选源码、选择状态与来源记录 | 是 |
| `configs/`、`evals/` | 通用配置与当前能力目标；每次运行另存冻结副本 | 是 |
| `tests/runtime/` | 验证固定部署逻辑，不承担 A3 自身的开发测试 | 是 |
| `reports/` | 面向读者的报告工程；审阅后的数据与图像和正文一起维护 | 源码是，构建输出否 |
| `third_party/` | 第三方原始源码与独立 Git 历史 | submodule |

人类提前准备固定基础设施和评估条件。专项 auto research Harness、Policy 候选、反思与选择由 SWE Agent 产生。整理候选快照时保留其原始代码，并记录来源；改进候选形成新版本，原始运行材料持续保留。

## 本地生成内容

| 路径 | 内容与生命周期 |
| --- | --- |
| `artifacts/rsi/run-*/` | 正式冷启动原始档案：冻结输入、独立工作区、候选、轨迹、实验报告与会话事件 |
| `artifacts/rsi/latest-run.txt` | 最近正式运行的路径，供轨迹浏览与恢复入口读取 |
| `artifacts/validation/` | 部署、恢复、隔离、预检、安装和报告复核记录；不作为正式验收的替代 |
| `artifacts/deployment/` | 镜像构建材料、迁移清单、部署修复与目录迁移记录 |
| `artifacts/remote-runtime/` | 执行端的作业、实验目录与共享资源锁；所有 Controller 必须使用同一配置根目录 |
| `reports/<name>/dist/` | 报告构建结果，由 Data 插件生成 |
| `reports/<name>/output/` | 报告截图与视觉检查输出 |
| `.env`、`.venv/` | 本机部署配置和依赖环境 |

`artifacts/` 中仅目录说明进入 Git。其余原始日志、工作区副本和部署配置留在本地。不要整体取消忽略；需要分享的候选源码移交到 `harnesses/`，需要展示的已审阅数据移交到 `reports/`。

## 正式运行内部结构

```text
artifacts/rsi/run-*/
├── inputs/                 当次冻结的目标、验收与执行条件
├── workspace/
│   ├── a3/                 初始构建 Agent 使用的 A3 副本
│   ├── harness/            独立项目 Git 工作区
│   └── run/                Agent 写入的实验记录与最终材料
├── public/a3/              后继候选可读的 A3 基线
├── candidates/             各后继的源码、轨迹和实验产物
├── fixtures/               受控隔离检查记录
├── recovery/               如有接续，保存传入的历史材料
├── trajectory.json         初始 mini 轨迹
├── model-usage.jsonl        Gateway 模型调用记录
└── session-events.jsonl     Host 记录的后继关系与生命周期
```

历史 `run-*` 内的文件用于复核，保持原样。运行结束后，由候选维护流程提取有价值的源码、说明和来源记录，原始日志仍留在原目录。恢复入口继续读取原路径。

## 候选源码与报告

`harnesses/phase-one/` 是冷启动生成的源码快照，详细状态见其 README。这里的保留版本仍依赖 Gateway、当次输入与交接材料，尚未打包为独立产品入口。第二阶段的逐候选 commit、并行 worktree 与评测绑定方案见[开发路线](roadmap.md)。

报告采用 Data 插件的工程结构：业务正文与图表放在 `src/content/`，审阅数据放在 `src/data.json`，共享 runtime 与构建校验文件保持插件原样。Data 的机器级预览服务状态留在仓库外。报告工程不复制进冷启动执行工作区。

## 迁移与清理约定

新增记录按用途写入已有分类，不以一次临时任务的名称继续扩展 `artifacts/` 顶层。验证输出放在 `validation/`，构建或迁移材料放在 `deployment/`。

移动历史档案前先检查活动进程与路径引用，记录旧路径、目标路径和内容校验结果；档案内原有绝对路径保留为当时的历史记录。更改便捷指针和当前使用说明。共享远程资源锁需要单独执行端迁移，不随本地目录整理移动。

清理时先确认保留的候选与原始证据已经独立保存，再决定过期检查和重复工作区的保留周期。源码归档与删除运行证据分开处理。
