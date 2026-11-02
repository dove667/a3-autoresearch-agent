# 第二阶段启动与衔接

Version: 0.8.0

本文件面向当前主 Agent 与候选接任主 Agent；普通 worker 的具体任务与停止条件由主 Agent 提供。长期行为与 Policy/Harness 选择原则见 SOUL.md；本文件不重复科学协议或接口手册。

## 校准当前任务

启动、恢复或接任时，读取当前 goal.md、acceptance.md、protocol.md、execution.md、输入清单与预算，校准自己的任务文件和待办，保留来源；人工恢复或输入变化后依真实状态重判旧终态。继承的第一阶段完成报告不是本轮完成条件；复用已有代码、工具、记忆与 skills，不重跑通用冷启动或 E/F 能力建设。先核实实际加载版本和可调用工具，缺失或具体失效时才按当前任务修补。

检索相关假设、已完成结果和明确未完成事项，核对真实在途作业，避免重复提交；重复实验记录复验理由。历史按需读取，不要求加载全部消息或补审前代。每代责任、执行前记忆与选择条件、开发子集和最终测试分别以 acceptance.md 与 protocol.md 为准。

学习型 Policy 优先适配、微调已准备且授权的基础模型，核对权重、backbone、tokenizer 与观测/动作接口；随机初始化的小网络可作链路验证或参考，不占据优化主线。方法仍自主选择，资源缺项准确记录并按 execution.md 处理，不自行下载。A3 仓库开发及其 pytest 回归属于维护者，本轮改进对象是自己的 Policy、Harness 与记忆。

按需使用已提供的 A3 autoresearch 与 RSI Harness skills；选择 Policy/Harness 工作时读取 RSI 的 `references/policy-harness-allocation.md`，研究方法、检索时机、技能维护和组件实验只加载相关参考。Skills 是可修订的经验，不是新验收项；文件存在不等于已加载或已执行。

## 执行、交接与结束

局部 worker、完整接任候选与主身份的行动和退出，统一遵循 execution.md 的“主身份与候选试运行”“独立候选执行”“整轮结束与未完成交接”；恢复与跨代资产分别查“上下文 checkpoint 与 resume”“大文件只读交接”。科学交付与各代责任按 acceptance.md 核对，不以局部测试通过替代整轮完成。

主流程不自行设置总 step 或总代数终止上限，在本轮预算内持续推进；预算不足按 protocol.md 停止新增实验并保存真实未完成状态。终态声明不代替实际退出，按 execution.md 保存恢复材料后结束自身循环。

## 反馈与输入变化

反馈影响实验安排时按 acceptance.md C 与 protocol.md §3 处理；生效输入按 execution.md“固定核心提示词”一致读取。SOUL 的反馈原则继续适用，不在此重复。
