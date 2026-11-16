---
name: a3-job-recovery
description: 提交和恢复A3远程作业：源码附件、CPU预检、提交未知、排队、部分回合与失败重试。Job submission, preflight and
  recovery.
metadata:
  version: 0.2.0
---

# 提交与接续研究作业

适用：提交研究脚本、提交结果不明、长期排队、执行失败或评测只完成一部分。先确认作业身份，再决定等待、修复或重试。

## 提交源码

`command` 使用参数数组，源码放在 `scripts/`，实验配置放在 `experiments/`，都可作为附件。`attachments` 附加真实文件并由工具计算哈希；已有大数据和权重通过已授权 `input_exports` 使用。

```json
{"command":["python","scripts/act/shared/diagnostics/inspect_data.py"],"gpu":false,"attachments":[{"path":"scripts/act/shared/diagnostics/inspect_data.py"}]}
```

使用当前 `job_submit` 工具的其他资源参数。CPU 作业直接提交；GPU 作业先用 `job_prepare` 保存请求并提交 CPU 预检，等返回的 `preflight_job_id` 完成后只用 `job_submit(prepared_id=...)` 提交。保存返回的 job ID、源码回执和预期结果位置，绑定到 plan 后再等待。

独立作业可以在一次调用里批量提交：`job_prepare(jobs=[...])` 一次预检多个 GPU 请求，`job_submit(prepared_ids=[...])` 或 `job_submit(jobs=[...])` 一次提交多个，返回逐项结果。部分失败不影响已成功的项，逐项读取 error 后只补做失败项。

## CPU 预检必须实现（GPU 提交硬门）

`job_submit` 在拿到 `a3-preflight.json` 之前不会提交 GPU 作业。生产脚本必须自己处理预检分支：真实导入依赖、加载数据与父 checkpoint，做一次最小前向或数据读取，然后写出报告并退出，不写正式训练产物。

```python
import json, os, sys

def preflight():
    # 必须真实执行：导入依赖、加载数据与基座/父 checkpoint、最小前向或读取。
    model = load_policy(config)          # 真实加载
    batch = next(iter(loader))           # 真实读取
    model(batch["observation"])          # 最小前向
    with open("a3-preflight.json", "w") as stream:
        json.dump({"schema": 1, "status": "passed"}, stream)
    sys.exit(0)

if os.environ.get("A3_RESEARCH_PREFLIGHT") == "1":
    preflight()
```

预检命令由 Harness 自动加前缀：`env A3_RESEARCH_PREFLIGHT=1 <你的命令>`。预检作业 `completed` 且 exit 0、报告 `schema=1,status=passed` 后，`job_submit(prepared_id=...)` 才会真正提交。预检失败时修正脚本后重新 `job_prepare`；附件内容变化会使旧 `prepared_id` 失效。

## 按状态处理

| 观察到的状态 | 下一步 |
| --- | --- |
| `queued`、资源紧张 | 保留同一ID，用 `job_wait` 等待调度。 |
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
