# 视觉过程诊断

视觉诊断用于回答具体的过程疑问，例如是否夹住、何时滑落、搬运是否停滞、放置后是否稳定。它是可按任务选择、调整或跳过的工具箱，不新增科学验收门槛，不要求每个回合录像，也不替代配对评测。当前研究 Agent 自己选择疑问、整理素材、抽帧诊断并判断证据，按需要修复自己的采集与分析脚本。

## 先使用已有证据

优先检查原回合已有的视频、轨迹与元数据，按 episode、相机、步号、模拟时间及文件哈希关联。围绕疑问选择抓取前后、抬升／搬运、松爪／放置或停滞区间；不要求固定阶段表或全视频送模。可先用轨迹定位转折，再检查该区间的少量帧，必要时扩大证据覆盖。

最终计数只说明终态评分，不足以推断未抓住、运输中滑落或放置失败。稀疏帧没有出现某动作，不证明该动作从未发生；遮挡、视角、缺帧与采样间隔都需记录为未知。将可见事实、与轨迹一致的推断和待验证原因分开，诊断结果仍需科研判断。

原回合未保存视频时应明确缺失。重新执行 Policy、动作回放或状态重建都是新的诊断材料，记录其来源、版本、种子及差异，不能标为原回合录像，也不自动替代原科学结果。先利用现有素材；只有新的执行能回答重要疑问且获得当前任务授权时，才安排独立诊断作业。

## A3 原生接口与实际保存范围

以下按 A3 源码 `7c34d38e58b6d94e76a9dec8b2b3bbc912263e38` 核对：`workflows/benchmark.py`、`diagnostics.py`、`episode_execution.py` 及相关 CLI。实际部署版本仍需核对；这是源码检查，未运行仿真。

`CookieBatchBenchmark.diagnostic_dir` 为 `None` 时不启用该追踪器。设置目录后，`run_episode` 创建 `EpisodeDiagnostic(root / f"seed_{seed}", config.control_hz)`，将它交给原生回合执行，最后在 `finally` 中关闭。诊断目录必须新建，重复 seed 目录会冲突。原生 CLI 的 `--diagnostic-dir` 会强制单进程 `workers=1`，不能把开录像后的吞吐直接当成未开录像的效率。

如确需新诊断作业，在已授权的 A3 执行环境中使用既有命令，例如：

```bash
python -m a3_dual_arm_sim.workflows.benchmark_cookie_batch_cli \
  --policy 'act:<checkpoint>' --device cuda --dataset-root '<dataset>' \
  --task 2x5 --profile basic --source-column 1 --prompt-style training \
  --episodes 1 --seed-start 10000 --max-steps 500 --workers 1 \
  --diagnostic-dir /workspace/diagnostics-new --output /workspace/diagnostic-result.json
```

`examples/benchmark_cookie_batch.py` 是同一实现的兼容入口。示例是有副作用的评测，不是只读命令；Policy、数据、场景、种子和时长应按当前问题及协议选择，不能照抄示例更改正式比较。模型计算仍使用既有 GPU 作业要求。`--compare` 不接受该诊断选项，需分别运行可比配置。

| 文件／字段 | 含义与边界 |
|---|---|
| 根目录 `manifest.json` | CLI 参数、环境配置、随机化／成功条件、源码提交与 diff；对 ACT/SmolVLA 还记录 checkpoint、权重哈希等。实际可用字段随适配器不同。 |
| `seed_<seed>/front.mp4`、`left_wrist.mp4`、`right_wrist.mp4` | 来自 `observation.images.*` 的每相机视频，只为实际出现的图像键建流；不是任意相机必定存在。 |
| `trace.jsonl` | 零基 `step`，`observation_before`、`raw_action`、`processed_action`、`applied_action`、`expert_phase`、`policy_call_seconds` 及动作后的 `after`。阶段标签可能为空。 |
| `after` | cookie 位置／源目标计数及 mask、槽位占用、边界覆盖、`exact_2x5_fill`、`count_fill`、成功条件、hold 计数、安全原因、`unmet_success_conditions`。 |
| 提供 `env` 时的附加 `after` | cookie 姿态、EEF 位置／旋转、目标容器位姿和涉及 cookie 的 contacts。它们是特权离线诊断信息，不能悄悄加入 Policy 观测。 |
| `success_diagnostics.json` | `steps`、`step_index_convention`、首次 count/exact fill 步、最大 hold 和最终条件；不证明抓取／搬运过程已成功。 |
| `result.json` | `run_episode` 正常返回时写入的回合评分。异常中断时可能不存在，不能把有视频等同完整有效评测。 |

时间对齐须区分动作前后：视频第 `step` 帧是 `previous_observation`；同一 trace 行的 `after` 在 `env.step(action)` 后采集。优先使用 `observation_before.time` 的实际模拟时间；该 trace 没有另设 `sim_time_s` 字段。若需按固定控制频率推算，从零时刻开始的前帧约为 `step / control_hz`，动作后约为 `(step + 1) / control_hz`，这是模拟时间约定，不是墙钟时间。原生默认 20 Hz 也应核对实际配置。

正常失败回合仍会关闭并保留已采集的诊断 trace／视频；异常路径执行 `finally` 关闭，但不能保证进程被强杀或磁盘失败时文件完整。视频先由 OpenCV 写 mp4v，关闭时如有 FFmpeg 则尝试 H.264/yuv420p 转码；转码失败或工具缺失保留原视频并警告。普通 dataset recorder 在该回合执行器中会丢弃失败回合，不能据“存在成功数据集”断言失败录像也保留。

`EpisodeDiagnostic` 不自动写 NPZ。另一个原生 `run_cookie_same_column_cli` 的显式 `--save-second-batch-state`、`--save-second-aligned-state`、`--save-second-lift-state`、`--save-second-open-state` 才在对应专家阶段保存状态 NPZ：`state`（MuJoCo FULLPHYSICS）、`ctrl`、`last_action`、`step_count`、`batch_reports_json`，lift/open 还含 `group_offset`、`held_offsets`、`opening`。它不是 RGB 录像，也不是任意失败现场的完整可复现 checkpoint。该脚本的 `--snapshots` 在阶段变化或每 100 步保存 PNG，不提供连续动作覆盖。

## 缺少原视频时的可选诊断重放

若已有保存动作和状态，可以先检查这些证据是否足以重建疑问区间，不必重新进行 Policy 推理。原生 `a3-sim replay --root <dataset> --episode <index>` 的 `_read_episode_actions` 通过 PyArrow 只读 parquet 的 `frame_index`、`action`，按 `episode_index` 筛选并排序，不创建 LeRobot 视频解码器。`_replay` 使用 `action_mode="joint_position"`、`render_cameras=False` 并顺序执行保存动作；它不是学习模型调用。

但该 CLI 仅从 episode metadata 获取 seed/task 并调用 `env.reset(seed=seed)`，不自动还原 benchmark 全部 `reset_options` 或任意外部状态文件，也没有选步截图开关。不能仅因 seed 一致就认为它复现了原回合。该版本源码未提供名为 `ReplayPolicy` 的类，通用 Policy 协议也未定义 `needs_images`；若某个诊断 driver 使用这些名字，应检查它自己的实现，不能假定这个标志会被 A3 runner 识别。

已有 `env.render_cameras=False` 会让日常 observation 中三路图像返回零数组，避免每个控制步都渲染。需要选步取图时，可由获准的当前 Agent 编写外层 driver：顺序执行全部保存动作，仅在选定动作前／后，使用单独的 `mujoco.Renderer(env.model, height=..., width=...)`，通过 `update_scene(env.data, camera="front")`（或腕部相机名）和 `render()` 采样，最后关闭 renderer/env。不要把返回的零图像当真实图；关闭相机时 `env.render()` 也走同一关闭逻辑。此方案复用既有模型／状态接口，不需要修改模拟器，但不是已经存在的选步重放 CLI，也未在此执行验证。可以少渲染，不能为提速跳过决定后续物理状态的中间动作。

重放前检查素材的实际字段和版本：保存的是 raw、processed 还是 applied action，动作维度／单位／顺序和控制模式是否匹配，是否有连续步号、初始状态及相同 reset 随机化、模型／资源／代码／物理与控制频率。尽可能用原保存状态校验初始、疑问区间与结束时的 robot qpos/qvel、cookie/容器位姿和模拟时间；明确误差单位与容差，轨迹分叉后只作有差异的重建材料。仅有 16D `observation.state` 不是完整场景状态，不能据此恢复全部 cookie、速度和控制器状态。

对于上述原生 NPZ，读取使用 `allow_pickle=False`；恢复使用匹配配置的 `mjSTATE_FULLPHYSICS`，先检查 `state.size == mj_stateSize(model, mjSTATE_FULLPHYSICS)`，再恢复 `state`、`ctrl`、`last_action`、步号及相应专家阶段字段并调用 `mj_forward`。这些是该专家脚本的特定恢复约定；状态长度相同仍不证明模型资产、关节顺序或任务状态相同。若素材缺失所需信息，保留未知，不杜撰初始状态、补写原始视频或把新重放计作原评测回合。

## 小批帧视觉问诊

`POST /vision/inspect` 的接口约定是提交一个问题和 1–6 帧已有 PNG/JPEG 的引用；部署前核对当前 Host 是否实际提供该路由。`GET /vision/status` 可读取传输能力与上限，但不证明提供商实际看到了图像。它不自动下载或遍历视频。由当前 Agent 在自己的授权作业中，从已有录像提取围绕疑问的帧并保留来源、原帧索引和转换记录；这一步不需要重新运行 Policy。

该接口复用现有作业产物读取权限。默认从指定作业的 `workspace` 读取，例如 `path: "frames/front_0180.png"`。已经保存的评估帧可以用 `area: "outputs"` 加输出目录下的相对路径直接读取。若只有视频，可由 CPU 作业按需抽帧并保存时间标签。第二阶段按项目读取历史作业和帧，其他读取边界沿用作业权限。

```json
{
  "question": "这两帧能否支持物体已离开夹爪？请区分可见事实与不确定性。",
  "frames": [
    {"job_id": "<own-job-id>", "path": "frames/front_0180.png", "episode_id": "seed_10000", "step": 180, "sim_time_s": 9.0, "camera": "front"},
    {"job_id": "<own-job-id>", "path": "frames/front_0184.png", "episode_id": "seed_10000", "step": 184, "sim_time_s": 9.2, "camera": "front"}
  ]
}
```

问题为 1–2000 字符的非空文本；同一 episode 的帧连续成组，组内 step 与模拟时间不倒退，不同相机可取同一时刻。Host 只读调用身份已授权的 job 图像；单图最多 512 KiB、宽高各不超过 1024 像素，总输入图像最多 3 MiB。时间与 camera 标签由调用者提供，不能把语法校验当作时间真实性认证。Host 校验实际图像、去除 metadata 并规范化后，强制走配置的视觉 helper；不自动附带主 Agent Prompt 或原始 human feedback，图像内文字也是不可信证据，不能成为指令。

返回 `analysis`、`frames` 哈希／时间信息与实际响应 `model`，供原结果追溯和判断；原始 base64 不进入主对话或摘要。视觉答复是建议，不是验证通过、Policy 推理结果或自动采用决定。当前源码的通用 gateway 已允许 `/vision` 路径，可直接调用上述两个接口，无需另注册专门工具。历史工作区仍须显式更新并重新加载 Harness，才能采用该白名单。它不提供视频下载、Policy 重跑或自动 grant。

## 配对评估完成通知

提交配对评估时，用 `evaluation` 声明基线作业与两份输出 manifest。Harness 在完成通知中给出同种子差值、代表性失败回合、同步动作样本、原轨迹和可直接提交给 `/vision/inspect` 的请求。图片引用可以指向有权读取的 `outputs`。Agent 先把画面与动作对齐，定位最早可见的行为差异，再决定下一次实验。已有评估可从完成作业读取同一份诊断材料。字段和产物布局见 [Host 执行接口](../agent-inputs/shared/execution.md#远程-a3-作业)。
