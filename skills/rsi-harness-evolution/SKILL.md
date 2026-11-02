---
name: rsi-harness-evolution
description: Diagnose and improve an Agent's own harness with fixed model weights. Use for context, memory, skills, tools, planning, scheduling, recovery, observability and version-selection experiments, with quality-preserving evidence. Complements A3 policy research rather than tuning the robot policy itself.
metadata:
  version: 0.1.0
---

# RSI Harness evolution

Make the research Agent more effective at its real work by improving the software around the model. A useful change removes a demonstrated bottleneck, enables a missing capability, or improves reliable task quality. More modules, more agents and more reflection are not inherently improvements.

**This skill is a starting repertoire, not an exhaustive search space or a checklist to finish.** Actively inspect your own trajectories, identify problems not listed here, invent alternative explanations and designs, and test them. Use the evidence to keep, revise or reject a candidate, including the advice in this skill. A listed technique may be unsuitable; an unlisted solution may be best. Preserve the current task and its scientific comparisons while exploring freely within it.

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

## Run an evidence-producing loop

1. **Observe and hypothesize.** Retrieve relevant prior attempts and in-flight work. Identify what would distinguish the suspected cause from alternatives.
2. **Define the comparison.** Freeze representative task inputs, initial state, expected deliverables, quality criteria and measured costs before execution. Use the optional [experiment record](assets/experiment-spec.json) or existing memory, not a second mandatory database.
3. **Change and exercise the real path.** Make a scoped candidate, test likely failure modes, and verify the running process actually loads it. A standalone utility test is not evidence that the full harness used the utility.
4. **Compare quality before cost.** Run matched workloads, preserve failures and unknown metrics, and separate external wait from controllable overhead. See [evaluation](references/evaluation.md). Do not obtain savings by removing required work, weakening evidence or changing the robot benchmark.
5. **Keep, revise or reject.** State the observed scope and uncertainty. A feature can be functionally useful without demonstrated efficiency gain. Use the existing trial/selection mechanism when adopting a complete main harness; do not invent another ownership protocol.
6. **Preserve and learn.** Save code/config/input versions, results, decision and recovery references. Update reusable guidance with applicability and counterexamples. The next generation inherits confirmed conclusions without re-auditing all predecessors.

Harness and policy research can proceed in parallel when they have independent dependencies and enough resources, but measure their contributions separately. Do not let speculative harness construction replace the actual research task. During a capability-only task such as G, use these methods to complete that task; the menu does not add phase-two experiments or acceptance requirements.

## What counts as progress

Distinguish `proposed`, `offline-functional`, `loaded-and-exercised`, and `paired-quality-preserving` evidence. Do not transfer a validated label to changed code, changed tasks or changed conditions automatically. No general capability or performance claim about a model follows from its name; profile the actual model–harness pair. Skill publication is not proof of runtime loading, and a paper's gain is not an A3 result.

This is a reference skill with an optional experiment template, not a runtime optimizer or new execution tool. Its [integration and validation notes](references/a3-integration.md) explain actual F discovery, reading and retention behavior.
