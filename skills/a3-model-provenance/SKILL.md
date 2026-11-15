---
name: a3-model-provenance
description: 模型与checkpoint谱系：父模型、权重哈希、数据来源、训练步骤及历史别名。Model identity, lineage and provenance.
metadata:
  version: 0.1.1
---

# 找到结果对应的真实模型

适用：checkpoint来源不清、多个历史模型别名、续训/转换/插值，或准备比较和保留Policy。

## 建立一条可复现记录

为每个实际可加载checkpoint保存稳定ID。最少记录：

| 内容 | 记录字段 |
| --- | --- |
| 权重 | 路径或export、SHA256、训练run与step、父checkpoint |
| 训练 | job ID、实际完成状态、数据/配置哈希、源码和依赖版本 |
| 推理 | 模型结构、adapter、任务映射或prompt、processor/tokenizer、归一化与相机处理 |
| 结果 | 评测条件、覆盖的任务/难度/seed、原始报告与有用能力 |

中间checkpoint按训练run和step区分。转换或插值额外记录父模型、转换方法和输出哈希。中断训练保留真实完成状态和可用权重。

## 对齐历史别名

一个实验标签可能包含多次训练、修复和评估。逐条将旧别名映射到真实checkpoint；收集、视频分析和评估作为关联工作，不另当作模型。把来源不明的部分标记为待核对，沿job、manifest和权重哈希继续查。

比较前核对checkpoint、adapter、数据和任务条件与声明一致。保留baseline、已有能力改进的候选及待验证替代模型。模型索引和实验材料放在工作区，下一步核对任务写入plan，结论或教训写入memory。
