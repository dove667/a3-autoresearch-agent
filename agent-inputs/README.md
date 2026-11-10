# 统一 Agent 输入目录

部署方给研究 Agent 的版本化源文件集中于此；本 README 只供维护者导航，**不在输入清单中，不注入模型**。

```text
agent-inputs/
├── SOUL.md
├── shared/
│   ├── bootstrap.yaml
│   ├── execution.md
│   ├── protocol.md  # 仅第二阶段选择加载
│   ├── workspace.md
│   └── readiness-task.md
├── phase-one/
│   ├── goal.md
│   ├── acceptance.md
│   └── instructions.md
├── phase-one-context/
│   ├── goal.md
│   ├── acceptance.md
│   ├── instructions.md
│   └── initial-feedback.md
├── phase-one-skills/
│   ├── goal.md
│   ├── acceptance.md
│   └── instructions.md
├── phase-one-web/
│   ├── goal.md
│   ├── acceptance.md
│   └── instructions.md
└── phase-two/
    ├── goal.md
    └── acceptance.md
```

Host 根据 phase 与 task_profile 选择一个阶段目录，并使用 SOUL 与共享接口文件。shared/protocol.md 只是存放位置共享，仍仅第二阶段加载，不加入第一阶段/G。容器内仍使用 `/run/input/{SOUL.md,goal.md,acceptance.md,phase-instructions.md,protocol.md,bootstrap.yaml,execution.md,readiness-task.md,workspace-instructions.md}`（phase-instructions 仅第一阶段必需，protocol 仅第二阶段必需；旧自定义快照可保留显式 phase-instructions）。完整映射和版本规则见 [输入加载说明](../docs/agent-inputs.md)，实现清单在 `src/a3_research_agent/host/inputs/versions.py`。

SOUL、goal、acceptance，以及第一阶段 instructions 或第二阶段 protocol 正文进入主 Agent／接任候选模型请求；其余文件保持只读可访问。普通 `/sessions` worker 被禁止，具体研究由当前 Agent 自己完成。`shared/workspace.md` 也用于构建工作区的 `harness/AGENTS.md`。研究 Agent 使用维护者部署的 Harness，具体边界见 SOUL。

文件名 SOUL.md 保留，仓库根不再维护重复副本。execution 统一维护于 `shared/execution.md`。各输入按实际内容独立维护版本。新 manifest 记录新源路径并产生新 revision，旧 run 的历史 manifest 与输入快照保留原样。

教程、审批规则和历史设计仍在 `docs/`；历史 plans 保留当时路径，不能视为当前加载映射。F 的部署方输入已放在 phase-one-skills/，由 skills-extension profile 选择；该 profile 验证维护者提供的技能工具，正式启动另行批准。设计来源见 [F 草案](../docs/plans/2026-11-02-phase-one-skills-f.md)。生成／retained Agent 的自有提示词、记忆和 skills 不属于此部署方目录，本次不改动。

G 的上下文管理输入位于 `phase-one-context/`，通过既有 `--run-harness` + `--agent-inputs-dir` 装配完整目录，不新增 profile。装配与初始反馈说明见 [输入加载说明](../docs/agent-inputs.md#g使用通用入口装配上下文管理任务)。G 输入准备不等于实际运行或能力通过。
