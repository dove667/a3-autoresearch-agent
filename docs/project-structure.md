# 目录职责与产物管理

项目将固定基础设施、原始运行档案与展示报告分别维护。先看项目能力与启动方式可读根目录 README；SWE Agent 生成的代码与运行证据共同保存在对应 run 中。

```text
.
├── runtime/
│   ├── local/             本地启动器、Gateway、候选版本、协议检查与恢复
│   └── remote/            远程作业 Controller、镜像构建与验证入口
├── configs/                通用启动配置
├── evals/                  当前目标与验收标准
├── tests/runtime/          本地和远程基础设施的回归测试
├── scripts/                薄启动入口
├── third_party/            固定版本的 mini-SWE-agent submodule
├── reports/                可选的本地报告工程，不纳入 Git
├── docs/                   环境指南、架构、路线与历史设计
└── artifacts/              本地运行档案、验证记录和部署材料
```

## 源码与职责

| 目录 | 负责什么 | Git 管理 |
| --- | --- | --- |
| `runtime/local/` | Host 固定的模型转发、身份与权限、候选生命周期、Git 版本、协议检查、诊断、冷启动和恢复 | 是 |
| `runtime/remote/` | 按归属运行远程 Docker 作业、分配资源、构建执行镜像 | 是 |
| `configs/`、`evals/` | 通用配置与当前能力目标；每次运行另存冻结副本 | 是 |
| `tests/runtime/` | 验证固定部署逻辑，不承担 A3 自身的开发测试 | 是 |
| `reports/` | 可选的本地报告工程，保存展示用正文、数据与图像 | 否 |
| `third_party/` | 第三方原始源码与独立 Git 历史 | submodule |

人类提前准备固定基础设施和评估条件。专项 auto research Harness、Policy 候选、记忆、反思与选择由 SWE Agent 产生，保存在对应运行中；改进候选形成新版本，原始运行材料持续保留。项目从源码 checkout 运行，`pyproject.toml` 管理依赖与工具配置，固定执行实现集中在 `runtime/`。

## 本地生成内容

| 路径 | 内容与生命周期 |
| --- | --- |
| `artifacts/rsi/run-*/` | 正式冷启动原始档案：冻结输入、独立工作区、候选、轨迹、实验报告与会话事件 |
| `artifacts/rsi/latest-run.txt` | 最近正式运行的路径，供轨迹浏览与恢复入口读取 |
| `artifacts/rsi/launch-logs/` | 后台启动时保存的终端日志；正式轨迹与验收证据仍归属各 run |
| `artifacts/validation/` | 部署、恢复、隔离、预检、安装和报告复核记录；不作为正式验收的替代 |
| `artifacts/deployment/` | 镜像构建材料、迁移清单、部署修复与目录迁移记录 |
| `artifacts/remote-runtime/` | 执行端的作业、实验目录与共享资源锁；所有 Controller 必须使用同一配置根目录 |
| `reports/<name>/dist/` | 报告构建结果，由 Data 插件生成 |
| `reports/<name>/output/` | 报告截图与视觉检查输出 |
| `.env`、`.venv/` | 本机部署配置和依赖环境 |

run 目录按 Host 本地时间命名，如 `run-20261031-110415-123456`；末段为微秒，发生重名时追加递增序号。正式运行与部署检查使用相同的命名规则。

`artifacts/` 中仅目录说明进入 Git。其余原始日志、工作区副本和部署配置留在本地。不要整体取消忽略；需要分享候选时，从明确选定的 run 与版本导出获授权的材料，需要展示的已审阅数据移交到 `reports/`。

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
├── versions/               Host Git 对象、候选元数据与独立 worktree
├── fixtures/               受控隔离检查记录
├── recovery/               mini 恢复时供构建者读取的历史
├── private-inputs/         主 Harness 独享的只读输入与跨轮历史
├── retained.json           Host 保存的明确保留结果及版本标识
├── trajectory.json         初始 mini 轨迹
├── supervision.json        Host 当前监督阶段与后继状态
├── supervisor-events.jsonl Host 监督与交接事件
├── outcome.json            Host 接受的整轮完成或外部阻塞声明
├── model-usage.jsonl        Gateway 模型调用记录
├── evolution.json          当前主身份、改动验收清单和保留/拒绝决定
├── session-events.jsonl     Host 记录的后继关系与生命周期
└── gateway-events.jsonl     请求开始、结束、错误类别与耗时
```

新的候选工作区实际位于 `versions/worktrees/<session_id>/source/`，`candidates/<session_id>/workspace` 是 Host 便捷链接。Docker 只挂载 source/；Git 元数据留在其父目录，不在挂载范围内。即刻修改要由 Agent 显式提交才能形成新版本。

历史 `run-*` 内的文件用于复核，保持原样。运行结束后，由候选维护流程提取有价值的源码、说明和来源记录，原始日志仍留在原目录。恢复入口继续读取原路径。

## 生成代码与报告

生成代码随 run 保存。运行使用 `versions/` 记录候选 commit，并以独立 worktree 执行；`retained.json` 定位 Agent 明确选择的结果。下一轮显式传入选定 run 目录即可加载，无需另外维护一份人工源码归档。评测绑定、版本选择和搜索策略由 Agent 使用 Host 接口组织，见[开发路线](roadmap.md)。

报告采用 Data 插件的工程结构：业务正文与图表放在 `src/content/`，审阅数据放在 `src/data.json`，共享 runtime 与构建校验文件保持插件原样。Data 的机器级预览服务状态留在仓库外。报告工程不复制进冷启动执行工作区。

整个 `reports/` 目录仅保留在本地，由 `.gitignore` 排除；克隆仓库后不会获得报告工程。公开文档不链接本地报告文件。

## 迁移与清理约定

新增记录按用途写入已有分类，不以一次临时任务的名称继续扩展 `artifacts/` 顶层。验证输出放在 `validation/`，构建或迁移材料放在 `deployment/`。

移动历史档案前先检查活动进程与路径引用，记录旧路径、目标路径和内容校验结果；档案内原有绝对路径保留为当时的历史记录。更改便捷指针和当前使用说明。共享远程资源锁需要单独执行端迁移，不随本地目录整理移动。

清理时先确认保留的候选与原始证据已经独立保存，再决定过期检查和重复工作区的保留周期。源码归档与删除运行证据分开处理。

跨轮接续直接使用选定的原始 run 目录。临时人工组装的交接包与重复源码副本，在确认原始代码和证据完整、自动历史交接可用且无活动引用后可以删除；`artifacts/rsi/` 中的原始档案继续供接续与验收。

启动流程与参数见[使用教程](tutorial.md)，执行单元和调用关系见[系统架构](architecture.md)。`continuation.py` 负责跨轮结果的机械保存与读取；`retained.json` 是运行结果，Agent 的记忆实现仍归属其生成的 Harness。

`configs/prompts/common.md` 是主 Agent 与接任候选的核心协议来源，每轮冻结到 `inputs/common-instructions.md`。测试 Agent 和普通 worker 不由 Host 自动加载该协议；其提示词属于主 Agent 的专项实现。
