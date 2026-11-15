# 数据、训练与控制诊断

按当前症状选择检查，完成后保存实际输入、结果和来源。先利用已有数据、日志和轨迹。

## 数据和时序

- 按完整episode及关联采集/场景组划分训练和评测，再生成时间窗口、拟合归一化统计。相邻帧跨split会泄漏近似相同的动作chunk。
- 抽查同步图像、state、原始预测与applied action：RGB/BGR、HWC/CHW、范围、相机顺序、时间戳及抓取/放置阶段。
- 对归一化做round-trip测试，记录常量维度、裁剪和超出训练范围的值。chunk时间padding不越过episode边界；检查短episode的起止索引和mask。
- 统计任务、难度、动作范围、失败与恢复覆盖。过滤规则保存为可撤销mask并记录原因；全部删除失败轨迹可能同时删去恢复行为。

## Task conditioning from data to actions

训练侧沿metadata → sampled batch → processor → forward核对，推理侧沿instruction → runner → adapter → forward核对。两侧任务映射、融合位置及归一化顺序一致。

ACT无语言输入时，可实现并联合训练task-ID条件；保存并重载embedding、映射和fusion配置。语言Policy检查真实tokens、mask和截断是否保留区分任务的部分。空指令或未知条件应明确报错。

检查实际梯度和真实模型调用；固定观察改变任务输入，随后做训练后的闭环比较。具体接入步骤用 `a3-policy-integration`；[ACT](act-chunking.md)和[VLA](vla.md)分别说明模型侧条件。

## 优化器与学习信号

小batch拟合用于定位学习路径。拟合失败时先查目标尺度、mask、梯度连接、冻结参数、增强和索引，再考虑扩大模型。

记录真实optimizer update数、样本数、各loss分量、LR组、梯度范数/裁剪比例、非有限值、scaler跳步、显存和输入等待。总loss下降可能掩盖夹爪错误或KL项占比。

AMP检查时先unscale再clip；梯度累积期间保持有效batch内scale一致。scheduler跟随真实完成的update。控制变量的FP32运行可定位数值问题；同时记录有效batch、样本顺序和完成工作量。

## Resume and artifact integrity

权重warm start和完整续训分别记录。完整续训核对optimizer、scheduler、scaler/EMA、RNG、sampler位置、global step、processor及代码/数据哈希。多worker预取会使精确中途恢复困难，选择可复现边界并做新进程重载检查。

MuJoCo重放还依赖完整积分、控制器、任务、RNG状态和版本，qpos/qvel可能不足。不要用未信任的pickle加载来检查元数据。

## 根据症状选择下一步

| 症状 | 先检查 |
| --- | --- |
| teacher-forced loss低，闭环立即失败 | processor反变换、arm顺序、inactive arm姿态、时间对齐 |
| 靠近成功，抓取或释放失败 | 夹爪时序、遮挡、动作年龄、clip；[视觉诊断](visual-diagnostics.md) |
| chunk边界跳变或动作迟钝 | 原始target连续性、队列年龄、执行前缀M和聚合 |
| 后期checkpoint变差 | 固定开发场景、数据重叠、过拟合及任务覆盖 |
| NaN或偶发大动作 | mask、梯度、scaler跳步、单位和物理尺度 |
| 训练日志不更新 | `a3-training-progress` |
| GPU利用率低或时间长 | 分开测decode、dataloader、推理、仿真和I/O；有解码瓶颈再用 `a3-frame-cache` |
| 入盒数高但success低 | 原生release、stability、boundary、slot等条件 |

记录测量的任务、horizon、CPU/GPU预算、并发和实际wall time；用完成的有效episode或update衡量吞吐。改变worker数或缓存时保留任务条件，避免把资源变化计为算法收益。
