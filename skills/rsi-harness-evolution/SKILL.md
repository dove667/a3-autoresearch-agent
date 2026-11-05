---
name: rsi-harness-evolution
description: Diagnose and improve an Agent's own harness with fixed model weights. Use for context, memory, skills, tools, planning, scheduling, recovery, observability and version-selection experiments, with quality-preserving evidence. Complements A3 policy research rather than tuning the robot policy itself.
metadata:
  version: 0.2.5
---

# RSI Harness evolution

Make the research Agent more effective at its real work by improving the software around the model. A useful change removes a demonstrated bottleneck, enables a missing capability, or improves reliable task quality. More modules, more agents and more reflection are not inherently improvements.

**This skill is a starting repertoire, not an exhaustive search space or a checklist to finish.** Actively inspect your own trajectories, identify problems not listed here, invent alternative explanations and designs, and test them. Use the evidence to keep, revise or reject a candidate, including the advice in this skill. A listed technique may be unsuitable; an unlisted solution may be best. Preserve the current task and its scientific comparisons while exploring freely within it.

## Choose the work before choosing a component

Use [Policy/Harness allocation](references/policy-harness-allocation.md) when deciding whether to run a policy experiment, improve the research harness, repair reliability, or wait. It supplies an evidence-driven starting policy, not a fixed work ratio or a new acceptance gate. Improve your own selection heuristics from observed outcomes; it does not authorize changes to Host-owned inputs. Capability-only tasks remain confined to their selected goal.

The main Agent coordinates and judges; Harness workers perform concrete implementation, testing and integration. Use [Research orchestration](../research-orchestrator/SKILL.md) for bounded assignments, evidence return, explicit asset grants and safe succession. Simple coordination does not require another worker. This reference does not install or prove automatic role enforcement in a running Harness.

## Find the next useful change

Read the active goal, acceptance and execution contract. Establish the actual running harness version and tools; repository templates or an edited file are not proof of loaded behavior. Use [A3 integration](references/a3-integration.md) to ground interfaces without importing a new framework.

Start from a concrete trace: an expensive repetition, wrong decision, lost fact, orphaned job, excessive wait, failed recovery, or a missing capability. Record a brief causal hypothesis and a competing explanation. For example, large prompts may result from unreadable tool output rather than weak memory; slow research may be simulator waiting rather than model reasoning. Do not rewrite the whole harness to fix an unlocalized symptom.

| Observed problem or opportunity | Candidate components | Reference |
|---|---|---|
| Growing context, forgotten active work, expensive repeated background | Working context, compaction, evidence retrieval | [Context and memory](references/context-memory.md) |
| Repeated mistakes or rediscovering procedures across experiments | Episodic memory, reusable skills, invalidation | [Context and memory](references/context-memory.md) |
| Wrong tool arguments, noisy output, redundant calls | Tool contracts, result shape, caching and error semantics | [Tools and observations](references/tools-observations.md) |
| Idle waits, duplicate experiments, unproductive decomposition | Plans, dependency scheduling, bounded parallel roles | [Control and scheduling](references/control-scheduling.md) |
| Lost jobs, duplicate side effects, broken restart or handoff | Recovery, checkpoints, idempotency and version loading | [Recovery and evolution](references/recovery-evolution.md) |
| A change seems cheaper but quality or causality is unclear | Instrumentation, paired workloads and selection | [Evaluation](references/evaluation.md) |

Load only the relevant references. Search primary sources when a mechanism or implementation is unclear; the [source map](references/sources.md) is not a requirement to read every paper. Borrow the smallest mechanism that answers the problem. Removing an unused abstraction or restoring a simpler baseline is a valid candidate.

## Develop and select useful changes

Choose a representative task that can reveal the change's effect, exercise the actual implementation and keep the results needed for your decision. Adapt the method to the question; the optional [experiment record](assets/experiment-spec.json) is not a required workflow or extra database.

A necessary repair or new capability can be useful without an efficiency gain. Judge it against its original purpose and integrate it when appropriate. Use matched quality and cost comparisons when assessing an efficiency claim, following the active scientific protocol. Do not save cost by dropping required work, changing the robot benchmark or losing recovery information. [Research orchestration](../research-orchestrator/SKILL.md) covers existing assignment and handoff boundaries.

Preserve the selected code, relevant results, unfinished work and recovery references. Revise methods from experience instead of re-auditing every predecessor or requiring every change to complete the whole run.

Harness and policy research can proceed in parallel when they have independent dependencies and enough resources, but measure their contributions separately. Do not let speculative harness construction replace the actual research task. During a capability-only task such as G, use these methods to complete that task; the menu does not add phase-two experiments or acceptance requirements.

## What counts as progress

Report what the tested change actually did and what remains unknown. Updated files are not proof of loaded behavior; results for another model, task or implementation do not establish this one's performance. Publication of a skill alone does not make a running Harness use it.

This is a reference skill with an optional experiment template, not a runtime optimizer or new execution tool. Its [integration and validation notes](references/a3-integration.md) explain actual F discovery, reading and retention behavior.
