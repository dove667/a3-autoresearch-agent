---
name: a3-job-recovery
description: 提交和恢复A3远程作业：源码附件、CPU预检、提交未知、排队、部分回合与失败重试。Job submission, preflight and
  recovery.
metadata:
  version: 0.1.3
---

# 提交与接续研究作业

适用：提交研究脚本、提交结果不明、长期排队、执行失败或评测只完成一部分。先确认作业身份，再决定等待、修复或重试。

## 提交源码

`command` 使用参数数组，工作区源码必须在 `scripts/`。`workspace_files` 附加真实文件并由工具计算哈希；已有大数据和权重通过已授权 `input_exports` 使用。

```json
{"command":["python","scripts/act/shared/diagnostics/inspect_data.py"],"gpu":false,"workspace_files":[{"path":"scripts/act/shared/diagnostics/inspect_data.py"}]}
```

使用当前 `job_submit` 工具的其他资源参数。GPU正式作业先按 `preflight` 的当前说明通过CPU预检。保存返回的job ID、源码回执和预期结果位置，绑定到plan后再等待。

## 按状态处理

| 观察到的状态 | 下一步 |
| --- | --- |
| `queued`、资源紧张 | 保留同一ID，用 `wait` 等待调度。 |
| 提交未知但返回ID | 用 `job_status` 查该ID；必要时用 `job_list` 核对当前授权清单。 |
| 传输失败且没有ID | 保存命令、源码哈希、时间与错误，核对授权清单和输出；身份仍不明确时请操作者处理。 |
| 终态执行错误 | 读日志、源码和episode ledger，修复已定位问题，仅补做未完成工作。 |
| 有效报告中 `success:false` | 保存为已完成的实验观测，继续分析失败原因。 |

输出暂时不存在、日志不动或等待重复返回，都应先查询原作业。不要直接提交等价副本。

## 恢复部分评测

1. 用 `job_status` 核对job与manifest身份，读取有界日志。
2. 根据manifest逐项核对episode ID、完成数、缺失、重复和异常条目。保留ledger及报告哈希。保留集只做运行健康检查时，不打开分数。
3. 读取当前execution提示词的episode-executor恢复说明；确认pending条目可沿用同manifest和输出目录。launching、running或结果未知条目先核对。
4. 用 `job_artifact` 分页读取结果。运行中文件可能增长，连续页面需验证来源一致；稳定完成报告保留整体哈希。

采集和评测使用部署方提供的 `A3_EPISODE_EXECUTOR`，路径从实际job环境取得。并发由现有资源预算约束；科学失败、执行失败与未知状态分别计数。
