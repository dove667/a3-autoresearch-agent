# 开发路线与实现状态

## 当前状态

项目已建立独立 Python 包与依赖管理，mini-SWE-agent 以固定提交的 Git submodule 集成。当前可使用第三方 mini 的交互入口；已准备目标、能力验收和无人交互启动配置，运行时从原始 mini 开始。

已提供冷启动容器、宿主模型转发与远程容器作业控制的部署边界。A3 专用研究工具、轨迹反思与自进化逻辑由运行中的 Agent 产生；能力完成情况按正式研究验收判断。

## 第一阶段：冷启动与基础自进化

为第二阶段验证四个能力：通过 A3 受限接口完成一轮 Policy 优化；根据自身执行轨迹完成一轮反思、Harness 候选修改及后续验证；以固定权限工具完成 auto research 并拒绝跨实验操作；新会话从持久记忆检索历史先例并影响实际行动。阶段一不设置任务表现、效率或成本的提升幅度门槛。

环境准备完成后，启动通用 mini，由它依据目标和验收反馈产生专项实现，再逐代改进。环境准备助手负责运行条件与评估准备。

冷启动构建环境与固定权限上限由部署方提供。生成后的auto research Agent 及其可演化代码在该边界内工作，权限边界和真实研究能力分别验收。

冷启动采用串行候选探索，保留分支并允许回退，不设总 step 或代数上限。模型调用复用 mini 的多提供商支持；默认执行方式为本地 runtime 通过 SSH 调用远程 GPU 环境，同时规划本地执行后端。

目标与验收分别见 [goal.md](../evals/goal.md) 和 [acceptance.md](../evals/acceptance.md)，无人交互运行见[冷启动说明](rsi-cold-start.md)。具体实现由 Agent 根据反馈决定。

## 第二阶段：Policy 与 Harness 联合优化

接入训练、检查点与模型评测，让 Agent 自主联合优化机器人 Policy 的任务表现和 Harness 的实验效率。评价纳入 GPU 时间、模型调用成本和达到目标成绩所需实验次数。

### 候选代码版本与并行执行

每个确定的 Harness 候选对应一个不可变 commit，正在修改的并行候选使用独立 Git worktree。候选记录包含父版本、auto research 假设与选择状态；每次评测绑定具体 commit、实验条件和结果。同一代码版本可以运行多次评测，候选版本树与运行会话树分别记录。

Host 管理共享 Git 仓库和 worktree，将获授权的候选源码交给对应 Docker 执行；共享 Git 对象库不直接暴露给各实验。代码版本、运行会话与 Controller 资源分配相互关联，便于复跑、回退、分叉和资源回收。

Host 已提供逐候选 commit、按身份授权的版本列表、独立 worktree 和全新身份启动入口。它们属于人类提前准备的基础设施。Agent 应显式保存选定源码，并将实际评测、协议、选择与版本关联；跨 Agent 分工、互相发现与避免重复实验的协调策略留待后续设计。

Host 也提供比较协议差异检查和脱敏请求诊断。记忆模块新增为阶段一 D 项，由 SWE Agent 自主实现与验收；历史 A/B/C 记录按当次冻结条件保留。第二阶段 Policy 任务条件与评测设计仍待团队调研，本次不实施。

## 代码与文档归属

完整目录职责见[项目结构](project-structure.md)。SWE Agent 已生成的源码快照归档到 `harnesses/`；网页报告工程位于 `reports/`；原始运行、验证和部署材料分别保存在 `artifacts/rsi/`、`artifacts/validation/`、`artifacts/deployment/`。

本项目源码、配置、测试和文档分别放在 `src/a3_research_agent/`、`configs/`、`tests/` 和 `docs/`。配置与测试目录随实现按需新增。仿真物理、任务计分和资源文件在独立的 `a3_dual_arm_sim` 仓库中维护。

第三方源码及原始文档放在 `third_party/mini-swe-agent/`。项目专属行为优先通过组合与子类实现；需要修改 mini 核心时，在 submodule 中维护独立提交，并确保团队可以取得该提交，具体约定见[第三方依赖说明](../third_party/README.md)。

## 自动接续与文档入口

`--run-harness <run目录>` 读取明确选定运行保留的快照、自动提供授权历史并采用当前目标；`--resume-from` 保持旧冻结目标并接续保存的代码与进展。保留声明由 Agent 输出，Host 负责机械保存；无需人工组装接续包。详细用法在 [tutorial.md](tutorial.md)，系统执行架构在 [architecture.md](architecture.md)，目录职责仍在 [project-structure.md](project-structure.md)。
