---
name: a3-policy-integration
description: 检查Policy接入：任务指令、ACT条件embedding、processor、adapter和哈希固定runner。Policy input,
  task conditioning and integration.
metadata:
  version: 0.1.2
---

# 检查Policy输入到动作的真实链路

适用：修改adapter、checkpoint、任务条件或生成的episode runner；模型看起来忽略指令；训练能运行但闭环立即失败。

## 先检查实际输入

1. 沿runner → adapter → processor → model forward查看真实传参；在模型边界记录任务文本或ID、tokens/mask、观察shape及归一化结果。
2. 训练与推理使用同一任务语义、映射、tokenizer/processor、截断和预处理。配置中有task字段时，还要确认它确实进入动作预测。
3. 对task-ID条件，检查embedding参与forward、梯度、优化器、checkpoint保存和重载。语言条件检查实际文本非空且区分任务的部分未被截断。
4. 固定观察改变任务输入，检查路径是否响应；训练后用条件匹配的闭环行为验证任务使用效果。历史无条件checkpoint按原条件保留。

## 更新哈希固定的源码链

checkpoint哈希可能写在adapter中，adapter哈希又写在episode源码及runner中。修改后逐条更新引用边，保存旧失败尝试与新源码哈希。

替换生成文本前核对旧哈希和唯一锚点，任一不符则停止修改、重新读取。编译声明任务和seed的所有生成变体，再用真实processor、adapter和输入运行预检，随后扩大作业。

比较修复效果时保留原物理、观察、seed与计分条件。根据ledger识别已完成、失败和未知episode，先核对未知提交再补跑。完整评测条件通过 `a3-mujoco-autoresearch` 的 `references/benchmark-evaluation.md` 按需读取。
