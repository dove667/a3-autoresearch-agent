# 研究工作区

Version: 0.4.0

本地原生进程只提供文件与研究工具，没有本地 bash 或 Python 执行入口。仿真、训练、评估及脚本执行通过远程作业完成。运行状态 `runtime_paths` 给出实际路径：

| 路径 | 用途与访问方式 |
| --- | --- |
| `workspace` | 唯一可写研究目录，文件工具的相对路径从这里解析 |
| `inputs` | 只读提示词与 `input-manifest.json` |
| `reference` | 只读 A3 参考源码、接口文档与 examples |
| `state` | 当前运行的程序档案，普通文件工具只读 |
| `skills` | 统一技能库，使用 `skills` 工具管理 |
| `memory` | 跨会话经验库，使用 `memory` 工具管理 |

研究脚本保存在 `research/`，以便通过 `workspace_files` 附加到远程作业。实验材料和自由工作文件保存在工作区；研究计划通过 `plan` 工具维护。暂停、恢复和升级 Harness 共用这个工作区；恢复后按计划核对真实作业与产物。

研究计划、接续与恢复清单、运行状态，以及 checkpoint、工具 journal、原始消息、执行日志、预检回执和上下文档案，均由 Harness 保存到当前 trajectory 的 `conversation/`。`context-history/...` 与 `.autorun-spill/...` 是相对 `runtime_paths.state` 的原文引用；使用 `recall` 检索，或拼接该根目录后用 `read` 分页核对。普通文件工具不能修改程序档案。

根目录 `skills/` 是唯一技能库，不复制到 workspace。通过技能工具搜索摘要、按需读正文与附件、创建和修改方法；通过 `memory` 工具检索、记录及修正长期经验。它们独立于对话和工作区保存，不随 Harness 发布覆盖。

`SOUL.md` 定义长期目标与研究原则，`execution.md` 说明工具操作，均由 `inputs` 保存版本快照。本文件在输入快照中名为 `workspace-instructions.md`。跨文件读取时核对 manifest 前后均为 active 且 revision/generation 一致；updating 时等待，版本变化后重读相关内容。
