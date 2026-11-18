# 独立 episode 执行器

远程作业通过固定执行器（由 `A3_EPISODE_EXECUTOR` 指定）组织采集与评估中的独立 episode。Agent 定义独立任务的命令、科研参数、种子、协议和输出；服务器 Controller 分配资源，执行器内部测量并调节并发。正常资源等待不唤醒研究模型。采集和评估统一使用此入口。

## 提交 batch

通过 `attachments` 发送入口脚本和 episode 代码。Controller 保留附件相对路径，并从作业工作区启动入口。下面的 `launch_batch.py` 从作业环境取执行器和持久输出路径，生成包含绝对报告路径的 manifest。提交命令为 `["python", "launch_batch.py"]`。

```python
import json
import os
import subprocess
from pathlib import Path

output = Path(os.environ["A3_OUTPUTS"]) / "example-batch"
output.mkdir(parents=True, exist_ok=True)
report = str(output / "episode-001.json")
manifest = output / "episodes.json"
manifest.write_text(
    json.dumps(
        {
            "version": 1,
            "episodes": [
                {
                    "id": "episode-001",
                    "command": [
                        "python", "scripts/act/baseline/evaluation/run_episode.py",
                        "--seed", "1", "--report", report,
                    ],
                    "report": report,
                }
            ],
        }
    ),
    encoding="utf-8",
)
subprocess.run(
    [
        os.environ["A3_PYTHON"],
        os.environ["A3_EPISODE_EXECUTOR"],
        "--manifest", str(manifest),
        "--output", str(output),
    ],
    check=True,
)
```

研究者提供实际的 episode 脚本，并让它写出声明的报告。manifest 支持 1–10000 个 episode。每项的 `id` 必须唯一，以字母或数字开头，其余可包含字母、数字、下划线或连字符，最长 96 字符。各项的 `report` 必须是不同的绝对路径，不能与 manifest、控制文件或日志冲突。`work_units` 默认 1，用于吞吐统计。

`max_parallel` 和 `threads_per_episode` 默认 1024 和 1，允许 1–1024。实际并发还受 episode 数和可用 CPU 的约束。`warmup_seconds`、`adjust_seconds` 默认均为 15 秒，允许 0.05–3600 秒。`idle_timeout_seconds` 默认 60 秒，允许 1–3600 秒。

每个新 batch 使用独立输出目录；跨作业恢复同一个 batch 时使用同一 manifest 和输出目录。学习型 Policy 或模型计算按[执行说明](../../prompt/execution.md)申请并确认 GPU。

## 并发与观测

执行器使用 `/proc/stat` 增量计算整机 CPU 负载，并根据 `/proc/meminfo` 的可用内存与 `A3_HOST_MEMORY_RESERVE_MB` 判断余量。Controller 为 GPU 作业填写 `A3_JOB_GPU_MEMORY_MB` 并每两秒写入 `A3_JOB_TELEMETRY` 指向的文件。驱动支持时，采样按作业后代进程归属计算和 EGL 显存，并将无法归属的驱动开销计入预算。整卡占用同时参与准入判断。

执行器根据资源余量和完成吞吐调整并发，不修改资源额度；降低并发时等待在途 episode 自然结束。每次资源观测独立检查准入，至多启动一个 episode。GPU 遥测报错、不可用或超过 10 秒时立即停止新增。实际吞吐取决于任务与共享资源，应使用独立验证任务和输出测量，不重跑已完成的科学 episode 测吞吐。

输出目录包含 `ledger.json`、`metrics.jsonl` 和 `summary.json`，分别保存执行状态、资源和吞吐观测、batch 汇总；每个 episode 的标准输出和标准错误分别写入 `<id>.stdout` 和 `<id>.stderr`。命令以退出码 0 结束且其报告为有效 JSON 对象时记为 complete；报告中的 `success:false` 仍是有效科学结果，按实际评测设置解读。

## 恢复与失败

托管作业收到 `A3_JOB_MANAGED=1` 后，资源或测量暂时不足时在执行器内持续等待，不因 `idle_timeout_seconds` 到期退出或唤醒模型。Controller 的时限、取消与存储保护继续生效。非托管作业仍在无活动 episode、仍有 pending 且达到该时限时以退出码 75 结束；资源恢复后可使用同一 manifest 和输出目录继续。失败或不确定项需先核查。

同一 batch 的排他锁阻止并发运行。执行器记录 manifest 摘要，恢复时核对摘要和已完成报告哈希，并跳过通过校验的完成项。状态不确定的 launching/running 项与失败项拒绝重复运行；失败项不自动重试。先核查进程、产物和日志，再明确处理失败或不确定状态，避免重复采集或覆盖结果。

episode 主进程退出后若仍有后台后代，执行器清理其进程组并将该项记为失败。收到退出信号时执行器清理本次启动的子进程，保留已写入的结果；在途项按退出码和报告记账，未完成项记为失败，尚未启动的项保留 pending。再次使用 batch 前先核查失败项。远程作业状态与产物按[执行接口](../../prompt/execution.md)查询；使用 `area=outputs` 获取 `A3_OUTPUTS` 下的文件。
