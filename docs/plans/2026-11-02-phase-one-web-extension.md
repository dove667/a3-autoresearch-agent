# 第一阶段保留 Harness 的 E-only 联网扩展

Version: 0.1.0

本文记录 E 扩展的原始方案与当时路径。现行输入位于 `agent-inputs/phase-one-web/`，文件映射与加载规则见[输入管理](../agent-inputs.md)。

## 决定与边界

用新的第一阶段 web-extension run 沿用明确保留的 Harness，只补 E 联网工具；不 resume 已 self-stop 的旧 run，不回到通用 mini 冷启动。普通 phase-one 的 A–D 目标与验收不变；第二阶段科学协议、指标和阈值不变。

新副本保留代码、持久记忆、恢复与身份机制，初始化新模型上下文及 checkpoint；源快照不改，旧记录只读按需查询。历史 A–D 已记录结论继承来源与适用条件，状态为 historical-not-recertified。原 Host 未完成归档、历史缺口、retained readiness 不冒充本轮独立 A–D 审计；E 通过不认证原整轮历史。每代只对接手后的执行与决定负责，不给后继附加祖先全审计。

## 输入与启动

```bash
bash scripts/start-rsi.sh --phase phase-one --task-profile web-extension \
  --run-harness "artifacts/rsi/<第一阶段保留run-id>"
```

- 独立版本化输入为 `evals/phase-one-web/goal.md`、`acceptance.md` 与 `configs/prompts/phase-one-web.md`，初始均为 0.1.0；对应正文注入模型，execution 保持可读，不叠加普通 A–D Prompt。
- 验收只有 `## E.` 能力项。startup 与 input manifest 记录 task_profile；旧记录默认 standard。热加载保持所选 profile；resume 继承 profile 并拒绝冲突，不重置本轮上下文。
- 仅接受第一阶段 retained 来源，不接受 E 冷启动／阶段二。task_context 的 inherited_acceptance 记录 checks、historical-not-recertified、source_run 和 source_version，不作为新通过证据。
- 新副本如带旧 `run-outcome.json`，初始化为 continue 并要求新 E 证据，以免旧 complete／blocked 直接终止；task_context.retired_terminal 保存原摘要和来源。只处理新副本，不改源快照或同 run 恢复状态。
- 源入口检查发现旧任务文件、append-only 大轨迹和旧完成进度也会污染 fresh run：新副本将已知旧任务状态归档到 `inherited-task-state/` 并记录摘要，活动 task.txt 改为 E 目标，trajectory 清空，旧 progress 转为继承记录；checkpoint 初始化新消息。源快照与长期记忆保持不动，同 run resume 不清空本轮状态。
- 归档／初始化只处理已有 task.txt、trajectory.jsonl、progress.md、run-outcome.json；缺失文件不创建。task_context.initialized_task_files 逐文件记录 source_sha256、archive_path、source_run、source_version；路径冲突失败关闭。progress.md 活动内容为 E pending 与历史未重新认证，原文留在归档。
- 项目根目录私有 `.env`／`A3_RSI_ENV_FILE` 加载方式不变，不收集或展示 key。启动及运行干预授权维护在操作者文档，不注入 Agent 目标。
- 可选 `--initial-feedback-file <已批准文件>` 仅用于新建第一阶段 web-extension retained run，不接受 resume 或旧 `--feedback-file` 混用。非空 UTF-8、最多 64 KiB；fresh checkpoint 的 E 目标后加第二个 user message，新副本留 `initial-feedback.md` 与 task_context 摘要／字节数／来源。省略时仍只有目标，不固定注入私人反馈，也不混入动态 Host feedback 事件；原审批规则不变。

## 实现与验收职责

保留 Harness 自行实现 status/search/fetch function schema、注册、分派、结果解析、来源存储／引用和稳定错误处理；Host 不代写专项工具。必要时加载候选，按现有生命周期协议处理主身份变化，不强制完整 H0/H1 演化或 A–D 重跑。

源入口每轮写出的旧 resume 清单仅有三个文件，可能覆盖手工补充的清单并遗漏新增模块。E Agent 须自己修改保存逻辑，维护 resume／continuation 中的新工具模块、记忆和必要源码／来源引用，局部验证完整性、可达性与实际加载；外层 Host 不代写此实现，不追加历史 A–D 审计。

实际模型必须收到工具并成功 basic search，一般公开 A3／机器人学习问题至少 fetch 一个公开 HTML／纯文本正文，保存 URL、检索时间、实际调用和谨慎解释。摘要不是全文，不盲用超参数，不将公开成绩算作本地收益。未授权、私有 URL、disabled、provider 认证／额度／限流用离线 fixture 验证，禁止真实滥用或耗尽额度。凭据、私有源码／日志／地址不进入查询或模型，不用 hosted web search 或网络旁路。

账户余额未知，没有 per-run 累计上限；并发、超时、安全与资源限制不变。只做必要本地检查和 E 真实联网验证，不训练、下载模型／PDF、运行 A3／GPU 实验。E 完成后保存结果、可复用 retained 与 continuation 并退出；外部配置／key／额度／网络阻断则有限排查、记录 blocked 和恢复状态后退出，不无限等待。新保留结果可供第二阶段直接加载。

## 验证状态

本文件描述实现合同，不声称本地检查或真实联网验收已通过。离线回归与运行中的 E 证据分别记录；部署配置或 key 已配置的自报不能替代成功模型工具链路。启动入口修改不自动启动、恢复、热加载或中断任何运行。

离线验证：354 项测试通过；Ruff 与 git diff --check 通过，独立审查未发现阻断。覆盖 profile 隔离与恢复、新副本初始化、历史源不变、旧终态不能冒充 E 通过，以及显式初始反馈的边界与来源记录。真实模型工具链路须在获准的 E-only run 中另外验证。
