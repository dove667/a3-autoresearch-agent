# 开发路线与实现状态

## 当前状态

项目通过源码 checkout 与脚本运行，由 `pyproject.toml` 管理依赖，固定执行实现集中在 `src/a3_research_agent/`。mini-SWE-agent 以固定提交的 Git submodule 集成。当前可使用第三方 mini 的交互入口；已准备目标、能力验收和无人交互启动配置，可从原始 mini 冷启动或明确选定的保留 Harness 继续。

已提供冷启动容器、宿主模型转发与远程容器作业控制的部署边界。A3 研究工具由维护者部署，实验与轨迹由 Agent 产生；能力完成情况按正式研究验收判断。

## 第一阶段：研究能力验证

验证 Policy 实验、轨迹留存、隔离和研究知识复用。

## 第二阶段：学习型 Policy 研究

接入训练、检查点与模型评测，让 Agent 自主研究机器人 Policy 的任务表现。评价纳入 GPU 时间、模型调用成本和达到目标成绩所需实验次数。

[具体方案](plans/2026-10-31-phase-two.md)与独立的 [Policy/Harness 评估协议](../agent-inputs/shared/protocol.md)已写成设计稿：采用两个任务、三个难度的完整网格，模型、数据及训练方法由 Agent 自主选择，Harness 效率单独比较。两个阶段的目标与验收分开维护，第一阶段另有专项 Prompt，第二阶段直接组合 SOUL 与共享存放但仅该阶段加载的 protocol，阶段输入选择与恢复隔离已实现；离线依赖预检、预算控制、保留 Harness 的新上下文与只读大文件交接已实现；实际训练和评测状态由相应 run 的原始证据记录。

### 线性版本与远程并行作业

夜间由单个 Agent 自主研究 Policy、采集、训练、评估与分析结果。白天由人类和 Codex 依据原始执行轨迹维护 Harness，包括模型调用、工具、上下文、记忆机制、调度与恢复。Agent 负责报告过程问题及证据，并继续修改研究代码和管理研究知识。

Host 管理共享 Git 仓库和 worktree，将获授权的候选源码交给对应 Docker 执行；共享 Git 对象库不直接暴露给各实验。代码版本、运行会话与 Controller 资源分配相互关联，便于复跑、回退和资源回收。

Host 提供源码版本、独立工作区与恢复入口。维护者选择部署版本，Agent 记录实际运行版本、研究配置与结果。

Host 也提供比较协议差异检查和脱敏请求诊断。记忆模块新增为阶段一 D 项，由 SWE Agent 自主实现与验收：在当前 session 主动写入值得保存的真实经验，再由新会话检索复用；历史 A/B/C 记录按当次版本化条件保留。第二阶段评估已有独立设计稿；真实训练和正式评测待 Agent 执行。

## 代码与文档归属

完整目录职责见[项目结构](project-structure.md)。SWE Agent 生成的代码与原始运行材料共同保存在 `artifacts/rsi/`；可选网页报告工程在本地 `reports/` 维护并由 Git 忽略；验证和部署材料分别保存在 `artifacts/validation/`、`artifacts/deployment/`。

本项目固定基础设施、Agent 输入、测试和文档分别放在 `src/a3_research_agent/`、`agent-inputs/`、`tests/` 和 `docs/`。仿真物理、任务计分和资源文件在独立的 `a3_dual_arm_sim` 仓库中维护。

第三方源码及原始文档放在 `third_party/mini-swe-agent/`。项目专属行为优先通过组合与子类实现；需要修改 mini 核心时，在 submodule 中维护独立提交，并确保团队可以取得该提交，具体约定见[第三方依赖说明](../third_party/README.md)。

## 自动接续与文档入口

`--run-harness <run目录>` 读取明确选定运行保留的快照、自动提供授权历史；`--resume-from` 保留原身份、代码、对话和进展。两者都采用启动时的当前目标、验收与部署方 Prompt，记录版本。运行中的输入更新需要预览警告和显式确认，见 [Agent 输入版本](agent-inputs.md)。保留声明由 Agent 输出，Host 负责机械保存；无需人工组装接续包。

## 运行历史与恢复

保留各代源码、模型上下文、记忆、科研证据和显式资产授权，恢复前核对真实作业状态。当前运行不建立多 Agent 研究分支；历史 checkpoint 与旧 run 用于按需追溯，后续新任务由操作者明确选择保留版本。
