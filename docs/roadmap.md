# 开发路线与实现状态

## 当前状态

项目已建立独立 Python 包与依赖管理，mini-SWE-agent 以固定提交的 Git submodule 集成。当前可使用第三方 mini 的交互入口；已准备目标、能力验收和无人交互启动配置，运行时从原始 mini 开始。

A3 专用结构化工具、SSH 与本地执行后端、候选验收、自进化循环和专用 CLI 尚待实现。远程服务器与本地 GPU 的可用性由各部署环境单独验证。

## 第一阶段：冷启动与基础自进化

通过预设测试反馈和评估指标，让 Agent 完善自身 Harness，形成受限工具、实验工作流和持久记忆，完成一次有效的 Policy 改进尝试与经过新会话验证的 Harness 改进。

环境准备完成后，启动通用 mini，由它依据目标和验收反馈产生专项实现，再逐代改进。环境准备助手负责运行条件与评估准备。

冷启动采用串行候选探索，保留分支并允许回退，不设总 step 或代数上限。模型调用复用 mini 的多提供商支持；默认执行方式为本地 runtime 通过 SSH 调用远程 GPU 环境，同时规划本地执行后端。

目标与验收分别见 [goal.md](../evals/goal.md) 和 [acceptance.md](../evals/acceptance.md)，无人交互运行见[冷启动说明](rsi-cold-start.md)。具体实现由 Agent 根据反馈决定。

## 第二阶段：Policy 与 Harness 联合优化

接入训练、检查点与模型评测，让 Agent 自主联合优化机器人 Policy 的任务表现和 Harness 的实验效率。评价纳入 GPU 时间、模型调用成本和达到目标成绩所需实验次数。

## 代码与文档归属

本项目源码、配置、测试和文档分别放在 `src/a3_research_agent/`、`configs/`、`tests/` 和 `docs/`。配置与测试目录随实现按需新增。仿真物理、任务计分和资源文件在独立的 `a3_dual_arm_sim` 仓库中维护。

第三方源码及原始文档放在 `third_party/mini-swe-agent/`。项目专属行为优先通过组合与子类实现；需要修改 mini 核心时，在 submodule 中维护独立提交，并确保团队可以取得该提交，具体约定见[第三方依赖说明](../third_party/README.md)。
