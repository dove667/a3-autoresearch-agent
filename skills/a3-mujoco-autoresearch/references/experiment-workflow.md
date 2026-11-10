# Autonomous research and reusable experience

Sources checked 2026-11-02: [AIDE](https://arxiv.org/abs/2502.13138), [implementation](https://github.com/WecoAI/aideml), [MLAgentBench](https://arxiv.org/abs/2310.03302), [implementation](https://github.com/snap-stanford/MLAgentBench), [reliable RL evaluation](https://arxiv.org/abs/2108.13264), [rliable](https://github.com/google-research/rliable). Borrow experimental-search and uncertainty principles, not their whole harness or benchmark metrics.

## Research search instead of linear edits

Maintain a useful incumbent and a small evidence-backed frontier of alternatives. Branch from promising checkpoints/configs rather than always from the newest edit. Inventory supported trainable model families and cover unexplored methods with meaningful data and training budgets before concentrating on small edits to one checkpoint. Rank the next experiment by expected information, possible gain, feasibility and cost. A null result can prune a hypothesis; a broken job is not a refutation of the learning method.

Choose a cheap test appropriate to the question: static dataset audit for leakage, tiny fit for plumbing, learning curve for stability, same-weight deployment ablation for action age, paired rollouts for behavioral quality. Methods with slower startup may need different diagnostic windows; don't prune solely because another method improves faster initially. Equal updates, equal examples, equal GPU-hours and equal wall time answer different questions.

Write a concise record before execution: mechanism, changed variables, fixed comparator, expected observations, falsifying observation, selection rule, resource estimate and stopping conditions. Multi-component candidates are allowed, but attribute the result to the bundle until ablated. Preserve later plan changes and already observed outcomes; don't retrospectively rewrite the original prediction.

Use existing job state and persistent memory to avoid duplicate submissions. Failed, deferred, invalid, censored and completed experiments remain distinguishable. Save effective configuration and actual completed work, not only the intended command.

## Closed-loop decisions and uncertainty

Read the active protocol; its score and final-test rules supersede literature examples. Development subsets are legitimate and often cheaper than full grids. Freeze the final candidate before using final test. A selection set reused to tune models is development data regardless of its filename.

Pair the same scenes and fixed external conditions. Identical random-seed integers across architectures don't imply identical sampled action noise. Report effect magnitude, uncertainty, failures and cost. Repeated candidate search biases the observed winner upward; don't convert a one-seed lucky win into a general law.

A scenario bootstrap of one trained checkpoint addresses scenario variability, not training variability. Independent training seeds answer a different question. Do not count frames as independent episodes or repeated rollouts as independent training runs. A zero-width bootstrap from constant tiny samples does not demonstrate certainty. See [helper](evaluation-helper.md); multi-training-run hierarchy needs separate analysis.

The complete phase-two metric is six-cell equal-weight normalized final count; original success remains separate. Good offline loss or final-count gain without complete success should be described precisely, not silently relabeled solved.

Inspect real video/frames together with synchronized actions and states to compare grasp, transport, placement, stability and recovery. Anchor observations to steps and matched conditions; quantify the relevant behavior when feasible. Preserve a candidate with supported improvement in any useful capability, its weights/configuration and evidence, even when final aggregate score is unchanged. Keep uncertain visual signs as pending verification. Maintain several capability-specific candidates; aggregate-score improvement is not a prerequisite for retention. Report native benchmark results and behavioral improvements at their respective scopes, including tradeoffs.

## Research execution evidence

Freeze the Policy, processor, data, deployed Harness version, protocol, configuration and training seed. Run independent experiments in parallel. Report Harness failures to maintainers with trace locations, actual impact and reproduction details. Policy inference compute, simulation time, model token cost, tool overhead and external waiting are separate measurements.

## Memory into skills

Memory records what happened. Skills capture what can be reused, under which conditions. Save a conclusion with claim, evidence IDs, data/model/contract versions, applicability, uncertainty, counterexample and next discriminator. Search these before another run. Store large raw traces/checkpoints in authorized artifact storage, not the prompt.

Use evidence labels consistently:

- `literature`: externally reported mechanism/result.
- `implementation-inspected`: code path verified at a named revision.
- `offline-helper-tested`: local deterministic fixture behavior only.
- `local-rollout-tested`: an actual A3 rollout, not necessarily comparative improvement.
- `paired-local-improvement`: matched evidence under stated conditions and uncertainty.

Promote narrowly, e.g. “shorter execution prefix improved release on this checkpoint/data/scene set,” not “M=1 always best.” Retain counterexamples and retire obsolete advice. A new implementation invalidates stale helper evidence until relevant tests rerun. One generation audits its own new experiments and inherits prior confirmed conclusions; revisit ancestors only for a concrete contradiction, not a full historical archaeology requirement.

A useful handoff names the incumbent, frozen inputs, inherited conclusions, this generation's actual results/decisions, in-flight jobs, unresolved uncertainty and highest-value next actions. It does not require copying an ever-growing full dialogue to prove a skill works.
