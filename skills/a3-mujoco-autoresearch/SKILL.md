---
name: a3-mujoco-autoresearch
description: Develop and improve learned policies for the A3 dual-arm MuJoCo cookie-packing project. Use for method selection, ACT/Diffusion/Flow/VLA/world-model experiments, data and training diagnostics, paired closed-loop evaluation, and reusable research experience. Applies to research planning or authorized execution, not permission to start training or change the benchmark.
metadata:
  version: 0.1.0
---

# A3 MuJoCo autoresearch

Turn robot-learning knowledge into **testable A3 experiments and reusable capability**, not a paper list. This is an externally authored baseline skill; its method recommendations are not evidence of local performance. Use it to act autonomously within the current task and refine it when real results justify changes.

## Start with the actual task

Read the current run's goal, acceptance, protocol and feedback, then [project contract](references/project-contract.md). Verify the deployed A3 revision and model/data interfaces before applying a recipe. Repository templates and paper results are not the run's execution state. This skill neither replaces those inputs nor expands permissions.

For each next action, identify the limiting uncertainty: a broken adapter, insufficient data coverage, open-loop drift, underfitting, inference latency, or research-process overhead. Choose an experiment that can distinguish explanations. A cheaper interface test or deployment ablation can be more informative than another training run.

**A3 traps to check first:** absolute joint targets rather than deltas; 16D dual-arm versus published 8D left-arm action; inactive arm held at reset pose, not zero; observation state may include more than action channels; 20 Hz control; consistent camera preprocessing; commanded versus applied-action labels; chunk queues and reset; final count rather than transient peak. See the contract for source-qualified details.

## Select a research branch

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

## Conduct the experiment

1. Retrieve related results and in-flight jobs. Reuse matching baselines; retain rejected experiments and counterexamples.
2. Record the hypothesis, evidence, changed variables, fixed conditions, diagnostic observations, resource estimate and selection/stopping conditions **before** submitting. [Experiment template](assets/experiment-spec.json) is optional, not a new mandatory database.
3. Establish interface correctness and a meaningful learning/control signal before scaling. Use tiny-batch fitting, replay and short rollouts when they answer the current uncertainty; they do not count as benchmark success.
4. Execute the authorized experiment with immutable versions and linked job/output locations. If a job is merely slow, diagnose utilization and progress instead of duplicating it.
5. Compare matched closed-loop results. Training loss is a diagnostic, not the policy-selection objective. Report execution failures separately from genuine task failure.
6. Keep, reject, or mark inconclusive using the recorded conditions. State coverage, remaining uncertainty, and the next discriminating action. A null result can still produce a valuable reusable diagnostic.

Policy and Harness improvements may proceed in parallel when dependencies and resources permit. Attribute gains separately: a cheaper policy, fewer evaluation episodes or omitted evidence is not a Harness efficiency improvement. A later generation inherits confirmed conclusions and their boundaries; it need not re-audit all predecessors.

## Evaluate without moving the target

For the repository's phase-two protocol 0.3.0, development subsets are allowed. A formal complete grid is six task/difficulty cells × 20 episodes, same policy snapshot, paired baseline/candidate scenarios. Keep 2×5 at 500 steps and 2×10 at 3200, 20 Hz, and the declared task instructions/conditions. The final-test set is not a tuning set. Re-read the active protocol if it differs.

Use [evaluation helper](references/evaluation-helper.md) for strict offline comparison of **explicitly normalized** reports. It validates keys and scientific comparability, reports cell results, computes full-grid G only for complete balanced coverage, and resamples paired scenario seeds together. It does not run simulation, authenticate provenance, certify acceptance, or decide whether to adopt a candidate.

## Improve this skill from evidence

Promote a method into a locally validated recipe only after linking actual code/config/data versions and evidence. Keep evidence levels distinct: `literature`, `implementation-inspected`, `offline-helper-tested`, `local-rollout-tested`, and `paired-local-improvement`. Add applicability, counterexamples and invalidation conditions; amend or retire stale advice rather than append contradictory rules. Preserve raw evidence outside the skill and reference it. Do not turn a single seed's gain into a universal hyperparameter.

When retained or handed off, include this entrypoint, referenced files and helper code, plus any required skill catalog and dependency declarations. Follow the verified [F integration contract](references/integration.md) and supplied catalog fragment; the mere presence of this directory is not proof it is loaded by an Agent.
