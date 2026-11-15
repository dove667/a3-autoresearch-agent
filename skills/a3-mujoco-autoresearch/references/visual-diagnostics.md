# 用视频与动作轨迹定位失败

用于抓取、运输、放置、稳定性或停滞的具体问题。优先读取原episode已有视频和trace，选能区分解释的时间段。

## 对齐原始证据

1. 固定episode ID、任务、seed、checkpoint和相机，保存文件哈希及观察问题。
2. 选取失败前后时间段，将图像与raw、processed、applied action及状态对齐。时间从真实控制频率计算。
3. 检查手臂接近、夹爪闭合、运输、释放和终态；用同步信号定位可见变化。记录遮挡、间隔和未解释冲突。
4. 以原生计分记录最终数量和success；框内可见物块不直接替代计分。稀疏帧缺少某动作时，继续查对应区间。

## A3原生诊断记录

已检查A3 revision `7c34d38e58b6d94e76a9dec8b2b3bbc912263e38` 的 `CookieBatchBenchmark.diagnostic_dir` / `EpisodeDiagnostic`：CLI `--diagnostic-dir` 在正常返回时保存相机MP4、`trace.jsonl`、`success_diagnostics.json`和`result.json`，并使用workers=1。trace的零基step对应before-action图像，另记动作和after-step状态。部署前核对当前版本接口；突然终止可能留下部分文件。

缺少原录制时，新rollout或重放作为新的诊断记录。原生 `a3-sim replay` 从parquet读动作、关闭观察相机渲染，但只恢复其支持的seed/task条件，不自动恢复任意benchmark状态或导出指定帧。

需要选帧重构时，使用已授权driver推进每一步动力学，仅在目标step渲染指定相机；核对初态、动作约定、版本、控制频率和可用已保存状态。不要跳过中间step，也不要把重构帧标为原录制。

## 调用视觉辅助工具

通过 `gateway` 调用 `POST /vision/inspect`，正文提供 `question` 和1–6个按时间排列的 `frames`。每帧填写 `job_id`、相对 `path`、`episode_id`、`step`、`sim_time_s`、`camera`；图片在job工作区时用默认 `area=workspace`，在输出目录时填写 `area=outputs`。

每张PNG/JPEG最多512 KiB、边长最多1024像素，总量最多3 MiB。同一episode按step递增排列。保存返回的帧来源、模型和分析；结合原始动作与状态判断结论，避免把大段Base64或全部视频放进主上下文。当前execution提示词定义实际接口限制。
