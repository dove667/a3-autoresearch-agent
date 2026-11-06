# 独立 episode 执行器

远程作业通过固定执行器（process 中为环境变量 `A3_EPISODE_EXECUTOR`，Docker 中为只读挂载的 `/run/a3/episode_executor.py`）组织采集与评估中的独立 episode。Agent 定义独立任务的命令、科研参数、种子、协议和输出；服务器 Controller 分配资源，执行器内部测量并调节并发。正常资源等待不唤醒研究模型。采集和评估统一使用此入口。

## 提交 batch

将 manifest 和命令依赖的文件放入远程作业的 `/workspace`。例如 `research/episodes.json`：

```json
{
  "version": 1,
  "episodes": [
    {
      "id": "episode-001",
      "command": ["python", "research/run_episode.py", "--seed", "1", "--report", "/outputs/example-batch/episode-001.json"],
      "report": "/outputs/example-batch/episode-001.json",
      "work_units": 1
    }
  ],
  "max_parallel": 1024,
  "threads_per_episode": 1,
  "warmup_seconds": 15,
  "adjust_seconds": 15,
  "idle_timeout_seconds": 60
}
```

`episodes` 的每项有唯一 `id`、命令参数数组 `command` 和绝对 JSON 报告路径 `report`。`work_units` 可省略，默认 1；它用于表示完成工作量。示例中的 episode 程序和参数由研究者提供，程序应写出声明的报告。五个调节选项均可省略，上例为默认值。`max_parallel` 和 `threads_per_episode` 为 1–1024 的整数，`warmup_seconds` 和 `adjust_seconds` 为 0.05–3600 秒，`idle_timeout_seconds` 为 1–3600 秒；实际并发上限还受 episode 数与 CPU 配额除以每 episode 线程数的约束，至少为 1。manifest 支持 1–10000 个 episode；ID 以字母或数字开头，其余仅用字母、数字、下划线或连字符，最长 96 字符。报告路径须是彼此不同的绝对路径；推荐写入 `/outputs`。报告路径与 manifest、执行器控制文件、日志或临时文件冲突时，在写入前拒绝。

作业命令为：

```bash
python /run/a3/episode_executor.py --manifest research/episodes.json --output /outputs/example-batch
```

每个新 batch 使用独立输出目录；跨作业恢复同一个 batch 时使用同一 manifest 和输出目录。学习型 Policy 或模型计算按[执行说明](../agent-inputs/shared/execution.md#模型计算必须使用-gpu)申请并确认 GPU。

## 并发与观测

Docker 模式要求 cgroup v2 与有限内存额度，读取作业 cgroup 的 CPU、内存和进程额度，使用整个容器 `cpu.stat` 的增量衡量 CPU。GPU 显存预算和设备由 Host 提供的 `A3_JOB_GPU_MEMORY_MB` 与 `A3_JOB_GPU_UUID` 指定。Host 每两秒结合进程归属采样，并通过只读 `/run/a3/telemetry/resources.json` 提供 GPU 遥测。支持时从 NVIDIA XML 进程表同时归属计算与 EGL 渲染显存，同一 PID 去重；本作业承担自身进程显存及仍无法归属的驱动开销，其他作业已识别的渲染显存不重复计入本作业预算。驱动无法提供完整进程显存时退回保守估计，不将未知图形显存当作零。整卡实际占用仍独立限制准入。

执行器根据资源余量和完成吞吐调整并发，不修改资源额度；降低并发时等待在途 episode 自然结束。每次资源观测独立检查准入，至多启动一个 episode。GPU 遥测报错、不可用或超过 10 秒时立即停止新增。实际吞吐取决于任务与共享资源，应使用独立验证任务和输出测量，不重跑已完成的科学 episode 测吞吐。

输出目录包含 `ledger.json`、`metrics.jsonl` 和 `summary.json`，分别保存执行状态、资源和吞吐观测、batch 汇总；每个 episode 的标准输出和标准错误分别写入 `<id>.stdout` 和 `<id>.stderr`。命令以退出码 0 结束且其报告为有效 JSON 对象时记为 complete；报告中的 `success:false` 仍是有效科学结果，应根据研究协议解读。

## 恢复与失败

托管 process 作业收到 `A3_JOB_MANAGED=1` 后，资源或测量暂时不足时在执行器内持续等待，不因 `idle_timeout_seconds` 到期退出或唤醒模型。Controller 的时限、取消与存储保护继续生效。非托管作业仍在无活动 episode、仍有 pending 且达到该时限时以退出码 75 结束；资源恢复后可使用同一 manifest 和输出目录继续。失败或不确定项需先核查。

同一 batch 的排他锁阻止并发运行。执行器记录 manifest 摘要，恢复时核对摘要和已完成报告哈希，并跳过通过校验的完成项。状态不确定的 launching/running 项与失败项拒绝重复运行；失败项不自动重试。先核查进程、产物和日志，再明确处理失败或不确定状态，避免重复采集或覆盖结果。

episode 主进程退出后若仍有后台后代，执行器清理其进程组并将该项记为失败。收到退出信号时执行器清理本次启动的子进程，保留已写入的结果；在途项按退出码和报告记账，未完成项记为失败，尚未启动的项保留 pending。再次使用 batch 前先核查失败项。远程作业状态与产物按[执行接口](../agent-inputs/shared/execution.md#作业环境与接口)查询；`/outputs` 文件使用 `area=outputs` 获取。

## 进程后端

进程作业使用 `A3_EPISODE_EXECUTOR` 指定的同一执行器。先用 Python 从环境生成 manifest 的真实绝对报告路径，再以参数数组启动；路径变量不会自动在命令参数或 manifest 中展开。输出根目录使用 `A3_OUTPUTS`，无需 `/outputs` 别名。

执行器按 `/proc/stat` 增量观测整机 CPU，按 `/proc/meminfo` 的 MemAvailable 和 `A3_HOST_MEMORY_RESERVE_MB` 预留计算内存余量，不要求 cgroup 单作业额度。CPU、内存观测包含其他作业，压力升高时停止增加并发。GPU 遥测来自 `A3_JOB_TELEMETRY`，按继承作业标识核对本作业后代；超过10秒仍拒绝新增 GPU episode。完成台账、吞吐反馈和跳过已完成 episode 的语义一致。
