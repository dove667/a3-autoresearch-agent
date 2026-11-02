# 统一 Agent 输入目录

部署方给研究 Agent 的版本化源文件集中于此；本 README 只供维护者导航，**不在输入清单中，不注入模型**。

```text
agent-inputs/
├── SOUL.md
├── shared/
│   ├── bootstrap.yaml
│   ├── execution.md
│   ├── workspace.md
│   └── readiness-task.md
├── phase-one/
│   ├── goal.md
│   ├── acceptance.md
│   └── instructions.md
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
    ├── acceptance.md
    ├── protocol.md
    └── instructions.md
```

Host 根据 phase 与 task_profile 选择一个阶段目录，并共用 SOUL 和 shared。容器内仍使用 `/run/input/{SOUL.md,goal.md,acceptance.md,phase-instructions.md,protocol.md,bootstrap.yaml,execution.md,readiness-task.md,workspace-instructions.md}`（protocol 仅第二阶段）。完整映射和版本规则见 [输入加载说明](../docs/agent-inputs.md)，实现清单在 `runtime/local/input_versions.py`。

SOUL、所选阶段 instructions、goal、acceptance，以及第二阶段 protocol 正文进入主 Agent／接任候选模型请求；其余文件保持只读可访问。普通 worker 的任务仍由主 Agent 编写。`shared/workspace.md` 也用于构建工作区的 `harness/AGENTS.md`。目录整理不改变身份、权限、目标、验收或研究协议。

文件名 SOUL.md 保留，仓库根不再维护重复副本。execution 从 runtime/local/README.md 搬来，目录迁移时仅修正路径并升至 0.8.2；后续 F 接入说明升至 0.9.0。各输入按实际内容独立维护版本。新 manifest 记录新源路径并产生新 revision，旧 run 的历史 manifest 与输入快照保留原样。

教程、审批规则和历史设计仍在 `docs/`；历史 plans 保留当时路径，不能视为当前加载映射。F 的部署方输入已放在 phase-one-skills/，由 skills-extension profile 选择；准备输入不代表已运行 F 或已实现技能工具，正式启动另行批准。设计来源见 [F 草案](../docs/plans/2026-11-02-phase-one-skills-f.md)。生成／retained Agent 的自有提示词、记忆和 skills 不属于此部署方目录，本次不改动。
