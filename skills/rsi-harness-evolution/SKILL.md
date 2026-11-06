---
name: rsi-harness-evolution
description: Diagnose and improve an Agent's own harness with fixed model weights. Use for context, memory, skills, tools, planning, scheduling, recovery, observability and version-selection experiments, with quality-preserving evidence. Complements A3 policy research rather than tuning the robot policy itself.
metadata:
  version: 0.2.8
---

# RSI Harness evolution

Make the research Agent more effective at its real work by improving the software around the model. A useful change removes a demonstrated bottleneck, enables a missing capability, or improves reliable task quality. More modules, more agents and more reflection are not inherently improvements.

**This skill is a starting repertoire, not an exhaustive search space or a checklist to finish.** Actively inspect your own trajectories, identify problems not listed here, invent alternative explanations and designs, and test them. Use the evidence to keep, revise or reject a candidate, including the advice in this skill. A listed technique may be unsuitable; an unlisted solution may be best. Preserve the current task and its scientific comparisons while exploring freely within it.

## Choose the work before choosing a component

Use [Policy/Harness allocation](references/policy-harness-allocation.md) when deciding whether to run a policy experiment, improve the research harness, repair reliability, or wait. It supplies an evidence-driven starting policy, not a fixed work ratio or a new acceptance gate. Improve your own selection heuristics from observed outcomes; it does not authorize changes to Host-owned inputs. Capability-only tasks remain confined to their selected goal.

The current research Agent implements, tests and integrates its own Policy and Harness work. Ordinary `/sessions` workers and research delegation are unavailable. Use one linear formal trial for a complete Harness replacement: the predecessor waits for self-test results; the candidate self-tests and waits until rejection or adoption. Adopt the same candidate, retire the predecessor and preserve memory, evidence and explicit asset grants.

## Find the next useful change

Read the active goal, acceptance and execution contract. Establish the actual running harness version and tools; repository templates or an edited file are not proof of loaded behavior. Use [A3 integration](references/a3-integration.md) to ground interfaces without importing a new framework.

Start from a concrete trace: an expensive repetition, wrong decision, lost fact, orphaned job, excessive wait, failed recovery, or a missing capability. Record a brief causal hypothesis and a competing explanation. For example, large prompts may result from unreadable tool output rather than weak memory; slow research may be simulator waiting rather than model reasoning. Do not rewrite the whole harness to fix an unlocalized symptom.

| Observed problem or opportunity | Candidate components | Reference |
|---|---|---|
| Growing context, forgotten active work, expensive repeated background | Working context, compaction, evidence retrieval | [Context and memory](references/context-memory.md) |
| Repeated mistakes or rediscovering procedures across experiments | Episodic memory, reusable skills, invalidation | [Context and memory](references/context-memory.md) |
| Wrong tool arguments, noisy output, redundant calls | Tool contracts, result shape, caching and error semantics | [Tools and observations](references/tools-observations.md) |
| Idle waits, duplicate experiments, unproductive decomposition | Plans, dependency scheduling, parallel remote jobs | [Control and scheduling](references/control-scheduling.md) |
| Lost jobs, duplicate side effects, broken restart or handoff | Recovery, checkpoints, idempotency and version loading | [Recovery and evolution](references/recovery-evolution.md) |
| A change seems cheaper but quality or causality is unclear | Instrumentation, paired workloads and selection | [Evaluation](references/evaluation.md) |

Load only the relevant references. Search primary sources when a mechanism or implementation is unclear; the [source map](references/sources.md) is not a requirement to read every paper. Borrow the smallest mechanism that answers the problem. Removing an unused abstraction or restoring a simpler baseline is a valid candidate.

## Develop and select useful changes

Choose a representative task that can reveal the change's effect, exercise the actual implementation and keep the results needed for your decision. Adapt the method to the question; the optional [experiment record](assets/experiment-spec.json) is not a required workflow or extra database.

A necessary repair or new capability can be useful without an efficiency gain. Judge it against its original purpose and integrate it when appropriate. Use matched quality and cost comparisons when assessing an efficiency claim, following the active scientific protocol. Do not save cost by dropping required work, changing the robot benchmark or losing recovery information. Use the active execution contract for linear trials, explicit asset grants and safe handoff.

Preserve the selected code, relevant results, unfinished work and recovery references. Revise methods from experience instead of re-auditing every predecessor or requiring every change to complete the whole run.

Independent ready Policy or Harness remote jobs should be submitted in parallel within existing resource limits, then collected by the same Agent. Keep their contributions separately attributable. During a formal Harness trial the predecessor waits and the candidate does not start independent research before adoption. Do not let speculative harness construction replace the actual research task. During a capability-only task such as G, use these methods to complete that task; the menu does not add phase-two experiments or acceptance requirements.

## What counts as progress

Report what the tested change actually did and what remains unknown. Updated files are not proof of loaded behavior; results for another model, task or implementation do not establish this one's performance. Publication of a skill alone does not make a running Harness use it.

This is a reference skill with an optional experiment template, not a runtime optimizer or new execution tool. Its [integration and validation notes](references/a3-integration.md) explain actual F discovery, reading and retention behavior.

## Research access within one run

Registered research identities in the same run may read each other's code, memory, visible traces, logs, artifacts, job status and read-only exports already authorized to that run. Use the Host interfaces with your own token and keep provenance. Workspace writes remain owner-only; job cancellation and other management require ownership or adopted linear lineage. Other runs, external users and Host credentials remain protected. Analyzer reports are readable within the run; only the current main initiates analysis and records adoption decisions. The analyzer uses run-level aggregate statistics and has no execution tools.
