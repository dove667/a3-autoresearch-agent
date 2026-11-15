---
name: a3-mujoco-autoresearch
description: A3机器人策略实验：选择ACT、扩散/流、VLA方法，诊断数据与训练，做配对闭环评测。Policy methods, training diagnostics
  and paired evaluation.
metadata:
  version: 0.1.25
---

# A3 Policy 实验入口

用于选择 ACT、Diffusion/Flow、VLA 或 world-model 方法，以及排查训练与闭环评估问题。先确定当前问题，再读取下面对应的一份参考资料。

## 完成一次有用的实验

1. 用 `plan` 查看当前路线和已有作业，用 `memory` 搜索同模型、同症状的结果。确认权重、数据和现成评测是否可复用。
2. 写清要区分的解释、改动变量、固定条件和如何根据结果决定下一步。记录到计划；需要完整实验记录时使用 [实验模板](assets/experiment-spec.json)。
3. 确认真实数据和模型接口：动作维度、归一化、任务条件、相机与时间对齐。具体检查见 [A3接口](references/project-contract.md)，适配器改动使用 `a3-policy-integration`。
4. 修改源码后先用 `job_prepare` 保存GPU请求并做CPU预检，完成后用 `job_submit(prepared_id=...)` 提交保存的请求。独立且输入就绪的作业可以并行；已有作业未确认结束时先查询，避免重复训练或评估。
5. 按保存的条件配对比较新旧模型，分别记录入盒准确率和原生成功率；有希望的候选扩大到 [全量评测](references/benchmark-evaluation.md)。分析抓取、运输、放置或稳定性时，按需读取原视频和同步轨迹。
6. 用 `plan review` 记录继续、暂停或结束路线的决定。具体结论写入 `memory`；可复用的方法再更新 skill。保留模型、配置、作业和结果引用。

## 按问题读取

| 当前问题 | 需要的参考或 skill |
| --- | --- |
| ACT动作分块、执行前缀、时序聚合、低损失却闭环失败 | [ACT/chunking](references/act-chunking.md) |
| 多种有效动作被平均、扩散或流匹配策略 | [Diffusion/Flow](references/diffusion-flow.md) |
| 预训练视觉语言动作模型与任务指令 | [VLA适配](references/vla.md) |
| 有明确规划问题和转移数据，考虑MPC或latent actor–critic | [World models](references/world-models.md) |
| 数据、梯度、NaN、训练变差、时间或归一化问题 | [训练诊断](references/training-diagnostics.md) |
| 看视频定位抓取、搬运、放置失败 | [视觉过程诊断](references/visual-diagnostics.md) |
| 配对条件、候选选择和不确定性 | [实验比较](references/experiment-workflow.md) |
| 作业提交未知、排队或部分回合完成 | `a3-job-recovery` |
| 训练日志不更新或过大 | `a3-training-progress` |
| 反复视频解码成为瓶颈 | `a3-frame-cache` |
| checkpoint名称、父模型与数据谱系 | `a3-model-provenance` |

A3的关键接口：joint targets为绝对关节目标；8D左臂策略需保持右臂初始姿态，不能补零；16D双臂顺序与训练一致；任务条件必须真正进入动作预测；每次episode reset清空chunk状态。具体数值以部署版本和已保存设置为准。

只读取当前问题需要的资料。模型家族表提供可尝试方法，具体路线由已有结果和当前不确定性决定。[来源](references/sources.md)保留论文和已检查实现。
