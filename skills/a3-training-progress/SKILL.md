---
name: a3-training-progress
description: 检查训练是否卡住：大日志、step和loss、checkpoint、NaN、坏行与增长中文件。Training progress and
  log diagnosis.
metadata:
  version: 0.1.1
---

# 判断训练是否在推进

适用：训练日志不更新、日志太大、出现NaN或损坏记录。先读 `job_status` 的运行状态和日志尾部，再决定是否需要完整扫描。

## 最小检查

1. 比较两次观察的时间与step。step前进说明训练在执行；结合loss、checkpoint事件和耗时判断学习状态。
2. step不动时查stderr、最新checkpoint文件、数据读取和计算等待。没有checkpoint日志时核对实际输出，避免仅凭日志宣布丢失权重。
3. 需要扫描结构化日志时，提交已授权的只读CPU作业，逐行处理并仅保留少量错误例子。

扫描报告至少包含：源job/路径与哈希或字节区间、观察时间、读取字节/行数、最新step和loss、最新checkpoint、NaN/Inf数量、显式错误及坏行数量。JSON中的NaN/Inf用字符串或显式诊断字段表达，保持报告可解析。

## 处理仍在写入的文件

- 文件末尾的半行先标记pending，保存字节偏移，下次接着读；作业结束后仍不完整才计为坏行。
- 文件被截断、替换或前缀改变时重新建立扫描身份。增长中的整文件哈希会变，不要把不同版本页面直接拼接。
- 需要稳定完整扫描时读取已完成快照；保留原始日志供定位。

观察日志不改训练状态。确认需要修复或重启后，先核实job身份与已保存状态，再更新plan。训练loss下降用于诊断，闭环收益通过正式评估判断。
