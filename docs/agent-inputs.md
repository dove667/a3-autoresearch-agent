# Agent 输入的版本与加载

Version: 0.1.0

目标、验收、Prompt 和执行说明由部署方维护。新启动、`--resume-from` 和 `--run-harness` 都采用启动时仓库里的当前文件，包括未提交的工作区内容。恢复保留原 run、身份、生成的 Harness 和模型对话历史；部署方 system Prompt 与共性协议更新为当前版本。

## 需要维护版本的内容

| 源文件 | Agent 可读位置 |
| --- | --- |
| `evals/goal.md` | `/run/input/goal.md` |
| `evals/acceptance.md` | `/run/input/acceptance.md` |
| `configs/prompts/common.md` | `/run/input/common-instructions.md`，主 Agent 和接任候选的模型指令 |
| `configs/rsi-bootstrap.yaml` | `/run/input/bootstrap.yaml`，初始 mini 的 Prompt 与启动配置 |
| `runtime/local/README.md` | `/run/input/execution.md` |
| `runtime/local/readiness-task.md` | `/run/input/readiness-task.md`，部署验证任务 |
| `configs/prompts/workspace.md` | `/run/input/workspace-instructions.md`，构建工作区的 `harness/AGENTS.md` |

Markdown 在标题之后写 `Version: 0.1.0`；YAML 顶层写 `version: 0.1.0`。这些版本独立维护。任何内容修改都需要递增：兼容性文字修正递增 patch，例如 `0.1.0 → 0.1.1`；能力、协议或验收变化通常递增 minor；不兼容重构递增 major。版本号与 SHA256 同时记录。同一 run 见过的同名同版本文件若内容变化，加载会被拒绝，须先修正版本号。

新增直接发给 Agent 的文件时，添加明确版本，并将其加入 `runtime/local/input_versions.py` 的输入清单。专项 Harness 自己生成的任务、记忆和提示词由 Agent 管理，本规则约束部署方发布的输入。

## 启动与恢复记录

`inputs/input-manifest.json` 列出整套输入的快照编号 revision、发布尝试序号 generation，以及每个文件的源路径、语义版本和 SHA256。revision 定位一组确定的文件；generation 在每次发布尝试时增加，更新失败回到旧快照也会增加，让读取程序知道期间发生过更新。两者均由 Host 自动维护，开发者维护的是各文件的 `0.1.x` 版本。

发布开始时所有挂载先标记 status=updating（更新中），完成后才标记 active（可读取）。每次启动或更新的完整内容保存在 `input-revisions/revision-*/`；`input-events.jsonl` 保存切换记录。原始 `startup.json` 保持不变，恢复事件附带当前清单写入 `resume-events.jsonl`。旧运行首次升级时，原输入另存历史副本。

普通挂载与主会话的私有输入挂载同步发布。主 Agent 与接任候选每次模型请求都获得当前共性协议、目标和验收正文；普通测试 worker 的任务与 Prompt 继续由主 Agent 提供。用量日志记录请求采用的 input_revision、各文件版本与摘要。初始 mini 刷新部署方 system 消息，保留已有用户消息、模型回复和工具结果。

全部 worker 停止后，Host 先关闭更新入口，再按最终生效的验收版本作结构检查，并把该版本清单写入 outcome。历史结果保留各自产生时的版本；变更验收后需要 Agent 重新判断已有证据是否适用。

## 运行期间热加载

编辑源文件并递增版本不会自动改变正在运行的实验。先预览：

```bash
uv run python -m runtime.local.reload_inputs "artifacts/rsi/<run-id>"
```

命令显示警告、原版本、新版本和内容摘要，仅预览不更新。确认这些变化后，使用预览返回的完整摘要：

```bash
uv run python -m runtime.local.reload_inputs "artifacts/rsi/<run-id>" --confirm <预览摘要>
```

确认绑定具体文件内容。预览后源文件再次变化，旧确认会被拒绝。操作者认证只保存在 Host 私有状态中，Agent 的令牌不能调用这个更新入口。

已发出的模型请求按原版本返回，下一个请求使用新版本；对话历史和 Agent 自己的代码不重写。多文件读取前后必须都是 manifest.status=active，并且 revision 与 generation 都相同；updating 时等待，标识变化后重新读取。热加载更新文件与 Prompt，不重新创建已有的 Python 对象、不切换模型或 Docker 镜像；bootstrap 的运行参数变化需要重启 Host。固定基础设施代码更新也通过停止和恢复应用。

更新不自动启动额外 Agent、不替 Agent 选择实验、不回注人工验收结论。操作记录可用于复核何时、以哪个版本改变了任务条件。
