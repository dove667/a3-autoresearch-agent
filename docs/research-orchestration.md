# 单研究 Agent 工作流

夜间由单个 Agent 自主研究 Policy、采集、训练、评估与分析结果。白天由人类和 Codex 依据原始执行轨迹维护 Harness，包括模型调用、工具、上下文、记忆机制、调度与恢复。Agent 负责报告过程问题及证据，并继续修改研究代码和管理研究知识。

独立且输入就绪的远程作业并行提交，Controller 管理资源和等待。采集和评估共用 [episode 执行器](episode-executor.md)。Agent 保留假设、实际参数、模型与数据版本、原始证据、在途作业和下一步。暂停和恢复按[操作者监督](operator-supervision.md)执行。
