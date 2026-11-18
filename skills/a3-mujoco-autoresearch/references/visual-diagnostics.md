# 用视频与动作轨迹定位失败

用于抓取、运输、放置、稳定性或停滞的具体问题。优先读取原episode已有视频和trace，选能区分解释的时间段。

## 对齐原始证据

1. 固定episode ID、任务、seed、checkpoint和相机，保存文件哈希及观察问题。
2. 选取失败前后时间段，将图像与raw、processed、applied action及状态对齐。时间从真实控制频率计算。
3. 检查手臂接近、夹爪闭合、运输、释放和终态；用同步信号定位可见变化。记录遮挡、间隔和未解释冲突。
4. 以原生计分记录最终数量和success；框内可见物块不直接替代计分。稀疏帧缺少某动作时，继续查对应区间。

## A3原生诊断记录

已检查A3 revision `7c34d38e58b6d94e76a9dec8b2b3bbc912263e38` 的 `CookieBatchBenchmark.diagnostic_dir` / `EpisodeDiagnostic`：CLI `--diagnostic-dir` 在正常返回时保存相机MP4、`trace.jsonl`、`success_diagnostics.json`和`result.json`，并使用workers=1。trace的零基step对应before-action图像，另记动作和after-step状态。部署前核对当前版本接口；突然终止可能留下部分文件。

缺少原录制时，可用 `benchmark(diagnostics=true)` 针对代表性 seed 补录；回执给出每个 seed 的正面、左右腕部录像和轨迹路径。新 rollout 或重放保存为新的诊断记录。原生 `a3-sim replay` 从parquet读动作、关闭观察相机渲染，但只恢复其支持的seed/task条件，不自动恢复任意benchmark状态或导出指定帧。

需要选帧重构时，使用已授权driver推进每一步动力学，仅在目标step渲染指定相机；核对初态、动作约定、版本、控制频率和可用已保存状态。不要跳过中间step，也不要把重构帧标为原录制。

## 扫录像与复核关键帧

用 `vision_inspect` 扫过几个动作阶段，返回视觉助手的描述和来源。筛到影响结论的时间段后，用 `vision_view` 选择需要亲自复核的帧。主 Agent 在下一次决策看到保留分辨率的图像，并可结合同步轨迹判断。重看或加密采样时再次调用。

两种调用都需要 `question` 和按时间排列的 `frames`。每帧填写 `job_id`、相对 `path`、`episode_id`、`step`、`sim_time_s`、`camera`。area 默认 outputs，图片在作业工作区时可填 `area="workspace"`。每张 PNG/JPEG 上传源文件最多 512 KiB，边长最多 1024 像素。按总图像字节、模型上下文和上游限制安排批次。记录所用视角、step、接触和原生分数，便于复查。
