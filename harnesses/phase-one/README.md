# 阶段一生成的源码快照

这些文件由 SWE Agent 在冷启动中生成，从最终实验材料原样复制。来源文件和内容校验值记录在 `provenance.json`，便于追溯原始轨迹。

| 文件 | 内容 | 选择结果 |
| --- | --- | --- |
| `entry.py` | 包含 research 工具、模型调用循环、结果保存与后继请求的 Harness 入口 | 暂时保留 |
| `candidates/preview_baseline.py` | 独立对照中向模型展示长结果开头 10000 字符的版本 | 比较基线 |
| `candidates/preview_1500.py` | 保留相同操作，将上述展示长度改为 1500 字符 | 本轮拒绝 |
| `policies/reduced_motion.py` | 两臂运动命令乘 0.9，夹爪命令保持不变 | 本轮拒绝 |

`entry.py` 仍要求执行环境提供 Gateway、模型别名、独立身份、只读 A3 与任务输入，并引用历史交接材料。当前提供全新身份启动包装，历史实验的完整复现仍需要显式交接相应材料。

原始运行目录和文件路径由 `provenance.json` 的相对路径定位。原始材料继续保存在 `artifacts/rsi/`；目录整理没有修改这些源文件。归档过程保留生成代码的格式，项目自动格式化和 lint 不覆盖本目录。

## 新身份启动入口

人类提前准备了 `run.json` 与 `handoff.md`，用于向新身份加载原样 `entry.py`：

```bash
bash scripts/start-rsi.sh --run-harness harnesses/phase-one
```

从仓库根目录运行，沿用本地 `.env` 部署配置。Host 注入新认证与当前冻结输入，旧凭据不参与启动。
该入口从归档代码启动，不附带特定运行历史。跨轮继续演化使用 `bash scripts/start-rsi.sh --run-harness <run目录>`，由启动器读取上一轮明确保留的结果并自动提供历史，详见[教程](../../docs/tutorial.md)。A/B/C/D 正式验收以实际运行证据核对。
