# 研究工作区

Version: 0.8.0

本地原生进程只提供文件与研究工具，没有本地 bash 或 Python 执行入口。仿真、训练、评估及脚本执行通过远程作业完成。运行状态 `runtime_paths` 给出实际路径：

| 路径 | 用途与访问方式 |
| --- | --- |
| `workspace` | 唯一可写研究目录，文件工具的相对路径从这里解析 |
| `inputs` | 只读提示词与 `input-manifest.json` |
| `reference` | 只读 A3 参考源码、接口文档与 examples |
| `state` | 当前运行的程序档案，普通文件工具只读 |
| `plan` | 项目顶层 `plan/current.json`，使用 `plan` 工具管理 |
| `skills` | 统一技能库，使用 `skills` 工具管理 |
| `memory` | 跨会话经验库，使用 `memory` 工具管理 |

按以下约定组织研究文件：

- `scripts/<模型或方法>/<可选研究系列>/<用途>/` 保存源码，例如 `scripts/act/p142/training/`、`scripts/act/baseline/evaluation/`、`scripts/dagger/validation/`。研究系列仅在需要时增加一层；用途按实际内容命名，如 `data`、`training`、`evaluation`、`validation`；相关依赖放在一起，共用脚本复用，参数通过配置或命令传入。
- `experiments/<日期-实验名>/` 保存该实验的 `config.json`、作业与源码引用、`results/`、`figures/`、`videos/` 和 `analysis/`。配置记录模型版本、数据、超参数、seed 与评测设置；每次独立实验单独建目录。按实际产物创建子目录，源脚本统一留在 `scripts/`。

用 `workspace_files` 将 `scripts/` 中的源码附加到远程作业。需要多个依赖时一并附加；通过 `destination` 明确远程文件位置，并让命令、导入及相对路径与目标位置一致。大文件留在执行端，用 job/export ID 和哈希关联本地实验记录。每天的新 run 共用这个工作区、根计划、记忆和技能，使用新的模型上下文；启动后先读取共享计划，核对既有作业与产物。

项目顶层 `plan/current.json` 是跨运行共用的研究计划，`runtime_paths.plan` 给出文件路径。Agent 通过 `plan(action="show")` 查看、`plan(action="update", revision=..., ...)` 修改长期方向、研究路线和执行步骤，用 `plan(action="review", ...)` 保存结果复盘与路线决策，普通文件工具不直接修改它。新运行、恢复和 Harness 升级都读取这份最新计划；先核对计划中的既有 job，再决定后续操作。Harness 在当前 trajectory 的 `plan-snapshots/` 保存开始、修订和停止时的历史快照。

接续与恢复清单、运行状态，以及 checkpoint、工具 journal、原始消息、执行日志、预检回执和上下文档案，也由 Harness 保存到该 `conversation/`。`context-history/...` 与 `.autorun-spill/...` 是相对 `runtime_paths.state` 的原文引用；使用 `recall` 检索，或拼接该根目录后用 `read` 分页核对。普通文件工具不能修改程序档案。

根目录 `skills/` 是唯一技能库，不复制到 workspace。通过技能工具搜索摘要、按需读正文与附件、创建和修改方法；通过 `memory` 工具检索、记录及修正长期经验。它们独立于对话和工作区保存，不随 Harness 发布覆盖。

`SOUL.md` 定义长期目标与研究原则，`execution.md` 说明工具操作，均由 `inputs` 保存版本快照。本文件在输入快照中名为 `workspace-instructions.md`。跨文件读取时核对 manifest 前后均为 active 且 revision/generation 一致；updating 时等待，版本变化后重读相关内容。

长期记忆服务由 Host 随研究进程启动和关闭。根 `memory/` 保存经验修订与可重建语义索引，模型文件在项目 `model-cache/`；使用 `memory` 工具管理条目、适用运行和来源，普通文件工具不直接改动这些目录。
