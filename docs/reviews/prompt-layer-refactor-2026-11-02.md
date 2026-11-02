# Prompt ownership review: SOUL / Phase 2 / RSI skill

Source: `141f98a`. This is maintainer review material, **not an Agent input**.

## Result and canonical owners

SOUL keeps role, phase scope, concise evidence/autonomy principles and the Policy/Harness choice summary. Phase 2 remains a thin selected-stage bridge so no loader/schema/profile changes are required. Operational choice examples are an on-demand RSI reference, not a new always-injected manual.

| Old Phase 2 section / unique content | Canonical destination after refactor | Disposition |
|---|---|---|
| Intro and current-task calibration | Phase 2: 校准当前任务 | Keep new-task versus inherited completion distinction, actual loaded tools, source-preserving task update. |
| Small-subset iteration | Unchanged protocol §§1,3 and acceptance A | Remove duplicate; neither full-grid mandate per iteration nor weakened final test. |
| Long jobs / waiting → Harness priority | SOUL summary + RSI `policy-harness-allocation.md`; control-scheduling reference | Replace idle-time bias with approved bottleneck/value/information/cost/reuse baseline; meaningful packages, event-based replanning and optional parallelism. |
| Pretrained-first; random small model only reference/plumbing; adapt before missing-resource escalation | Phase 2: 校准当前任务 | Retain stage-specific guidance; not a new universal Phase 1 task. |
| Web API, quota/cache/errors/privacy/untrusted content | Unchanged execution: 研究网页网关 | Remove duplicate technical manual; no change to unlimited-per-run/provider quotas or privacy. |
| Search when evidence missing; source → testable local hypothesis | Existing A3 skill entry + experiment-workflow; protocol §§3,5 | Keep method owner; no mandatory search or E reimplementation. |
| Task memory, preregistration, result lineage, final budget | Unchanged protocol §§3–5, acceptance C/D; Phase 2 retrieval bridge | Remove duplicate; preserve before-submit memory and unfinished states. |
| Skills discovery, actual loading, no F rerun | Phase 2 bridge + existing skills' integration references | Retain actual capability check and targeted repairs only; no framework mandate. |
| Skill purpose, source/status/versions, counterexamples, retention | SOUL short principle + existing A3 skill Improve this skill / integration and RSI context-memory / integration | Remove repetitive recipe; optional skills remain outside fixed acceptance. |
| Per-generation responsibility and incomplete historical gaps | Unchanged acceptance: 代际责任与整轮覆盖; execution: 主身份与候选试运行 | Canonical requirements already cover own-work check, no ancestor audit, truthful missing coverage. |
| Workers versus full trials, actual tools, same-process transfer | Unchanged execution: 主身份与候选试运行 | Phase 2 explicit routing reference; preserve waiting candidate/old-main exit reminder. |
| Role-specific termination, saved state, self-stop last | Unchanged execution: 独立候选执行 / 整轮结束与未完成交接; acceptance D | Phase 2 points to actual contract; no new termination rule. |
| No total-step/generation cap | Phase 2: 执行、交接与结束 | Unique clause retained, still bounded by actual run budget. |
| Feedback interpretation, old plan/provenance, affected scope | Phase 2 routing: 反馈与输入变化; acceptance C; protocol §3; SOUL; execution feedback transport | Retain precise canonical pointers; SOUL owns honest intervention attribution, startup calibration covers reconsidering obsolete terminal state; no operator approval policy injected. |
| Active/revision/generation read consistency | Unchanged execution: 固定核心提示词 | Remove duplicate, retain explicit input-consistency pointer. |
| Checkpoint/replay, exports and Host role | Unchanged execution: checkpoint / 大文件只读交接 / version-selection sections | Remove duplicated API text; no live state or source snapshot changed. |
| Offline assets, A3 maintenance scope | Unchanged execution offline resources + Phase 2 scope sentence | Preserve distinct maintenance owner; no authority expansion. |

SOUL reductions consolidate repeated evidence/skills/feedback prose rather than deleting their principles. Existing skills already carry detailed evidence levels, applicability, failures and retention; this refactor does not relocate every removed paragraph into another bloated file. The new allocation reference is original design guidance, not an empirically validated efficiency claim.

## Verification boundaries

Only prompt/skill documentation and loader regression tests change. Scientific goal, acceptance and protocol files remain byte-identical. Runtime APIs, stage selection, model settings, resources, launch modes and execution.md are unchanged. G must still load only its custom G goal/acceptance/instructions plus shared SOUL; no Phase 2 task enters G. Skills remain optional files read on demand, not an implicitly injected dependency.

Prepared Mac G inputs are an old snapshot and need explicit regeneration after integration; editing this repository does not update them or any live run. No hot-load, run start, experiment, credential change or push is performed by this refactor.

## Completed review and checks

- Independent canonical-owner audit: no blocking lost rule; further removed repeated feedback and identity/exit explanations after review.
- SOUL: 3,693 → 2,410 UTF-8 bytes; Phase 2: 22,477 → 2,809 bytes. These are source sizes, not measured model tokens.
- 580 offline tests passed; Ruff, diff whitespace, skill frontmatter and relative-link checks passed. Real loader tests cover Phase 2 injection and G-only scope with updated SOUL.
- Legacy relocation tests now freeze the original SOUL and Phase 2 text instead of hashing mutable live files. Original migration hash expectations remain unchanged. Other wording assertions now check canonical owners rather than demand duplicate prose in Phase 2.
- This is deduplication plus the authorized replacement of the waiting-time scheduling bias, not a claim of unchanged scheduling behavior or measured efficiency gain.
