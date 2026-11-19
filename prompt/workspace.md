# 研究工作区

Version: 0.13.0

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

用 `attachments` 将工作区中的源码和实验配置附加到远程作业。需要多个依赖时一并附加；通过 `destination` 明确远程文件位置，并让命令、导入及相对路径与目标位置一致。大文件留在执行端，用 job/export ID 和哈希关联本地实验记录。每天的新 run 共用这个工作区、根计划、记忆和技能，使用新的模型上下文；启动后先读取共享计划，核对既有作业与产物。

数据与模型跨 run 共用。通过 `runtime_paths.remote_datasets`、`runtime_paths.remote_models` 获取远程根目录，远程代码从 `A3_DATASETS`、`A3_MODELS` 读取同一配置。按研究方向和来源组织，例如 `act/autoresearch/`、`moe-act/codex/`、`smolvla/autoresearch/`。通用基准和资源放在根目录下的 `shared/`。

复用历史资产时，先查相应研究方向的 `autoresearch/` 或 `codex/`，以及根目录的 `shared/`。常用数据集是 `shared/full-v1`、`shared/merged-2x5-2x10-v2` 和 `act/autoresearch/mixed-2x10-v1`。旧 ACT 权重可查模型根下的 `act/autoresearch/`。归属待核的历史资产在 `historical/autoresearch/`。

两个根目录下的 `a3-migration.json` 如存在，记录迁移资产的相对 `path` 及已有的旧 `source`。查具体数据集、checkpoint 或预处理产物时，通过 `job_submit` 提交只读 CPU Python 作业，按历史路径、资产名称或方向筛选清单，并将少量匹配结果写到 `A3_OUTPUTS`，再用 `job_artifact` 读取。清单只覆盖迁移时的资产，后续发布的版本用 Python 查看相应方向目录即可。

- 原始采集、合并集、可复用的预处理结果都放在数据根目录。用来源清单记录原始数据版本、采集作业与处理参数。
- 可复用的训练 checkpoint 放在模型根目录，附训练配方、来源数据和作业编号。
- 在所属方向的 `autoresearch/` 下用唯一版本目录暂存并校验，完成后在同一文件系统内发布到正式目录。已有版本和其他来源的资产只作输入。单次作业的日志、评测录像和诊断写入 `A3_OUTPUTS`，可重建的作业缓存使用 `A3_SHARED_CACHE`。

项目顶层 `plan/current.json` 是跨运行共用的研究计划，`runtime_paths.plan` 给出文件路径。Agent 通过 `plan(action="show")` 查看、`plan(action="update", revision=..., ...)` 修改长期方向、研究路线和执行步骤，用 `plan(action="review", ...)` 保存结果复盘与路线决策，普通文件工具不直接修改它。新运行、恢复和 Harness 升级都读取这份最新计划；先核对计划中的既有 job，再决定后续操作。Harness 在当前 trajectory 的 `plan-snapshots/` 保存开始、修订和停止时的历史快照。

接续与恢复清单、运行状态，以及 checkpoint、工具 journal、模型可见历史、执行日志、预检回执和上下文档案，也由 Harness 保存到该 `conversation/`。`context-history/...` 是相对 `runtime_paths.state` 的原文引用；`recall` 会沿引用检索完整工具输出，或拼接该根目录后用 `read` 分页核对。`research-receipts.json` 是程序保存的近期工具观察索引，普通文件工具只读。研究计划和结论仍分别使用 plan 和 memory 管理。

根目录 `skills/` 是唯一技能库，不复制到 workspace。通过技能工具搜索摘要、按需读正文与附件、创建和修改方法；通过 `memory` 工具检索、记录及修正长期经验。它们独立于对话和工作区保存，不随 Harness 发布覆盖。

`SOUL.md` 定义长期目标与研究原则，`execution.md` 说明工具操作，均由 `inputs` 保存版本快照。本文件在输入快照中名为 `workspace-instructions.md`。跨文件读取时核对 manifest 前后均为 active 且 revision/generation 一致；updating 时等待，版本变化后重读相关内容。

知识检索服务由 Host 随研究进程启动和关闭，memory 与 skills 共用一个本地 embedding 进程。根 `memory/` 保存经验修订与可重建语义索引，模型文件在项目 `model-cache/`；使用 `memory` 工具管理条目、适用运行和来源，普通文件工具不直接改动这些目录。

技能语义索引在 `.runtime/skill-index/`，经验向量缓存在 `memory/`。索引均可重建；skill 的正文和附件仍只保存在根 `skills/`，检索不会把整包注入上下文。
