# Agent 工作区说明

Version: 0.1.1

当前为独立 Docker 工作区。模型和作业经 Host gateway 转发，不得获取 Host 凭据或改变资源分配。
专项 auto research、Harness、Policy 与记忆由 Agent 自主实现。
读取 /run/input/goal.md、acceptance.md、execution.md 和 input-manifest.json 中的当前版本。
当前研究 Agent 自己完成 Policy 与 Harness 的实现、数据采集、训练、评估、脚本修复和验收判断。局部检查在本身份工具与远程作业中执行，不创建普通 `/sessions` worker，不采用 orchestrator/worker 分工。完整 Harness 版本只通过 `/evolution/trials` 启动一个线性候选；前任等待候选自测并读取结果，候选交权前仅完成清单并等待，不开展独立研究。拒绝则回收候选并继续原版本；采用则由同一候选接任，旧主保存交接后退出。禁止分叉并行研究 Agent。
互相独立且输入已就绪的远程作业，先在权限与资源额度内并行提交，再统一查询与收集结果；有数据依赖的作业按依赖推进。采集和评估中的独立 episode 统一使用 `/run/a3/episode_executor.py`，由 Agent 固定科学参数、seed、协议与输出；不另写固定两个并发的循环，不重复已完成的科学 episode 测吞吐。
普通脚本、参数、依赖适配和结果解析问题由当前 Agent 在当前任务中定位、修复并验证到底，保留失败与修复证据；需要 Host 凭据、权限、服务或资源配置变更时，保存具体错误、所缺条件和所需操作，明确报告操作者。正常作业等待按状态继续推进。
Host 提供主身份交接、结束自身和受控双身份验收接口。
