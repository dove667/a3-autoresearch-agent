---
name: a3-mujoco-autoresearch
description: Develop and improve learned policies for the A3 dual-arm MuJoCo cookie-packing project. Use for method selection, ACT/Diffusion/Flow/VLA/world-model experiments, data and training diagnostics, paired closed-loop evaluation, and reusable research experience. Applies to research planning or authorized execution, not permission to start training or change the benchmark.
metadata:
  version: 0.1.18
---

# A3 MuJoCo autoresearch

Turn robot-learning knowledge into **testable A3 experiments and reusable capability**. Follow SOUL's long-term goal: improve A3 2×5 and 2×10 full-evaluation accuracy and success rate. Refine this baseline skill when actual research results justify changes.

## Start with current research

Read SOUL, the saved research plan and relevant feedback, then [project contract](references/project-contract.md). Use [benchmark evaluation](references/benchmark-evaluation.md) for the full evaluation grid and saved comparison conditions. Verify the deployed A3 revision and model/data interfaces before applying a recipe.

For each next action, identify the limiting uncertainty: a broken adapter, insufficient data coverage, open-loop drift, underfitting, inference latency, or data-pipeline overhead. Choose an experiment that can distinguish explanations. Autonomously compare training, data improvement, and Policy deployment changes by their expected performance or information benefit, total time/compute and validation cost, risk and dependencies. A cheap interface test or deployment ablation is useful when it resolves a consequential uncertainty; cheapness alone does not justify repeating it instead of a more valuable training or data experiment. No direction or section order is a default optimum. Training need not be the expensive stage: if collection or closed-loop simulation dominates, optimize the measured pipeline bottleneck and reuse valid data/results rather than repeatedly recollecting or reevaluating.

**A3 traps to check first:** absolute joint targets rather than deltas; 16D dual-arm versus published 8D left-arm action; inactive arm held at reset pose, not zero; observation state may include more than action channels; 20 Hz control; consistent camera preprocessing; commanded versus applied-action labels; chunk queues and reset; final count rather than transient peak. See the contract for source-qualified details.

## Task conditions must reach the policy

For multi-task training, supply the declared task identity to the model's action-prediction computation in both training and inference. Task labels used only for sampling, logging or unused batch fields do not condition a policy. Use explicit conditions for available task labels rather than relying on scene appearance to imply the task.

For ACT without native language inputs, implement a trainable task-ID/embedding condition fused into the policy computation and train it jointly; preserve the task mapping, fusion location, preprocessing and embedding weights with the checkpoint. For SmolVLA, pi and other language-conditioned models, deliver the actual task text through the matching tokenizer/processor, tokens and mask in both paths. The runner and adapter must forward the instruction; reject missing/unknown task conditions for a conditioned model instead of silently substituting empty text.

Verify the actual training forward and deployed runner-to-adapter-to-model path. Check condition gradients where trainable, compare a fixed observation under different task conditions, and evaluate task-appropriate behavior after training. Changed outputs alone do not prove learned task following. Save the task map or prompt format, tokenizer revision, truncation policy and preprocessing with the model. See [training diagnostics](references/training-diagnostics.md#task-conditioning-from-data-to-actions), [ACT](references/act-chunking.md) and [VLA](references/vla.md). Historical unconditioned checkpoints keep their original meaning and results.

For a concrete adapter or instruction-delivery repair, read `a3-policy-integration` with the skills tool. It covers the deployed call chain and hash-pinned generated runners.

## Execute research in one Agent

The current Agent implements, trains, evaluates and fixes its own scripts. Do not create ordinary `/sessions` workers or delegate research to an orchestrator. Use the deployed Harness. Save reproduction details for tool, context, scheduling or recovery failures for the maintainers. Preserve scientific evidence, memory, resource limits and explicit cross-run asset grants.

Fix ordinary script and adapter failures within the current research and verify the repair. Report specific missing infrastructure permissions, services or resources to the operator.

## Schedule independent episodes

Submit independent ready remote jobs in parallel within the existing resource limits, then collect their results. Respect data dependencies. For collection and evaluation, submit an episode manifest to the deployment's unified executor:
use the `A3_EPISODE_EXECUTOR` and `A3_OUTPUTS` paths supplied to a process job; Docker uses `/run/a3/episode_executor.py` and `/outputs`. Build argument arrays from the actual job environment inside the entry script rather than copying paths from a previous backend.
Read the current execution reference's episode-executor section for the manifest and recovery contract.
Keep task parameters, seeds, policy weights and output paths explicit. Let the executor adjust concurrency
inside the job's existing CPU, memory and GPU reservation; avoid replacing it with a fixed two-process loop.
It limits per-episode numerical threads and observes the whole container plus Host GPU telemetry.
Scientific `success:false` remains a completed observation when the command and report are valid.
Reuse completed reports, reconcile uncertain submissions before retrying, and keep diagnostic throughput
runs separate from registered scientific evaluations. Compare valid episodes per wall time with matched
workloads; raising CPU use alone does not establish an improvement.

Read `a3-job-recovery` for uncertain submissions and batch health, `a3-training-progress` for large logs, and `a3-frame-cache` when repeated decoding is the measured bottleneck. Discover and read them with the skills tool.

## Select a research branch

**These references are not an exhaustive search space or a task checklist.** Actively discover problems in the actual project and trajectories, propose methods or hypotheses outside this menu, and test them. Keep, revise or reject candidates using evidence; update the skill itself when new findings contradict its advice. Do not limit exploration to the named model families or implement every listed method for its own sake.

These are options, not a mandatory ladder. Prefer the strongest executable baseline that fits current data, compute and target uncertainty; compare alternatives when the evidence warrants it.

| Situation | Useful starting branch | Read |
|---|---|---|
| Existing demonstrations and working chunk-policy adapter; cheap strong baseline | ACT, chunk consumption and temporal aggregation | [ACT/chunking](references/act-chunking.md) |
| Demonstrations contain multiple valid trajectories; mean-action behavior is harmful | Diffusion or conditional flow policy, with inference-latency accounting | [Diffusion/flow](references/diffusion-flow.md) |
| Existing compatible pretrained policy; visual/semantic generalization matters | SmolVLA or other VLA adaptation, with action and processor checks | [VLA](references/vla.md) |
| A concrete planning/long-horizon uncertainty and suitable transition/reward/goal data | World-model branch; distinguish MPC from latent actor–critic | [World models](references/world-models.md) |
| Any model fails, overfits or consumes budget without learning | Data, optimization, control and failure localization | [Training diagnostics](references/training-diagnostics.md) |
| Experiments or tool work are inefficient or hard to reproduce | Research search, attribution, memory and decisions | [Experiment workflow](references/experiment-workflow.md) |

Read only the branch and support material needed. [Sources](references/sources.md) distinguishes papers, implementation versions and local observations. Use web search to resolve a specific mechanism, reproduce an official implementation, or investigate a failure; do not collect citations indefinitely instead of testing a hypothesis.

For a concrete grasp, transfer, placement or stagnation question, prefer existing episode video and synchronized traces. [Training diagnostics](references/training-diagnostics.md#visual-process-evidence) describes optional native capture and bounded visual inspection. The research Agent prepares and inspects process evidence, then decides what it supports. Neither routine recording nor a new rollout is required. Sparse frames and final counts do not establish an unseen action never occurred; a replay is new evidence, not the original episode video.

## Preserve behavioral progress and explore model families

Keep candidates with demonstrated improvements in grasping, transport, placement, stability or recovery, even when aggregate score or complete success is unchanged. Inspect real video/frames with synchronized action/state traces and matched task conditions; report the specific capability, coverage, uncertainty and tradeoffs. Preserve weights/checkpoints, configuration and evidence for each useful capability. Keep uncertain visual improvements as candidates for targeted verification. Maintain useful alternatives and report native benchmark metrics with their actual evaluation conditions.

Inventory the actually trainable policy families, prior attempts and missing prerequisites. Give unexplored supported families meaningful data, training budgets and closed-loop diagnostics before repeatedly fine-tuning the same checkpoint. Tiny plumbing tests do not reject an algorithm family. Submit independent ready experiments within fixed quotas and reuse valid data, recordings and evaluations. See [research workflow](references/experiment-workflow.md) for candidate retention and process comparisons.

## Conduct the experiment

1. Retrieve related results and in-flight jobs. Reuse matching baselines; retain rejected experiments and counterexamples.
2. Record the hypothesis, evidence, changed variables, fixed conditions, diagnostic observations, resource estimate and selection/stopping conditions **before** submitting. [Experiment template](assets/experiment-spec.json) is optional, not a new mandatory database.
3. Establish interface correctness and a meaningful learning/control signal before scaling. Use tiny-batch fitting, replay and short rollouts when they answer the current uncertainty; they do not count as benchmark success.
4. Execute the authorized experiment with immutable versions and linked job/output locations. If a job is merely slow, diagnose utilization and progress instead of duplicating it.
5. Compare matched closed-loop results. Training loss is a diagnostic, not the policy-selection objective. Report execution failures separately from genuine task failure.
6. Preserve useful behavioral improvements and mark uncertain candidates for targeted verification using the recorded conditions; keep aggregate benchmark conclusions scoped to their evidence. State coverage, remaining uncertainty, and the next discriminating action. A null result can still produce a valuable reusable diagnostic.

Run independent Policy experiments in parallel when resources permit. Record the deployed Harness version and keep scientific conditions comparable.

## Evaluate model progress

Use [benchmark evaluation](references/benchmark-evaluation.md) for development subsets and the complete six-cell, 120-episode grid. Record scenarios, step limits, control frequency, task instructions and the full policy snapshot before a comparison. Use small subsets for daily iteration and complete evaluations for promising candidates; preserve a separate final comparison set.

The optional [evaluation helper](references/evaluation-helper.md) compares **explicitly normalized** reports. It validates keys and declared conditions, reports cell results and paired uncertainty, and can compute the historical full-grid scalar. Its schema is an analysis format. Select candidates from actual results and continue research; no fixed score or confidence threshold ends the project.

## Improve this skill from evidence

Promote a method into a locally validated recipe only after linking actual code/config/data versions and evidence. Keep evidence levels distinct: `literature`, `implementation-inspected`, `offline-helper-tested`, `local-rollout-tested`, and `paired-local-improvement`. Add applicability, counterexamples and invalidation conditions; amend or retire stale advice rather than append contradictory rules. Preserve raw evidence outside the skill and reference it. Do not turn a single seed's gain into a universal hyperparameter.

Use the single root skill library through the controlled skills tool: search summaries, read a relevant body, then read only the needed resources. Preserve methods and their attachments with the tool’s versioned save and archive operations. Follow the current [integration contract](references/integration.md). Read `a3-model-provenance` with the skills tool to link checkpoints, aliases, conditions and results.

## Research access

Use the shared project `workspace/`, `memory/entries.jsonl` and Host-authorized jobs and exports. Keep source provenance when reusing data or results, and report missing access through the operator. Future sessions and maintenance releases reuse these directories; remote job management still checks the recorded research identity.
