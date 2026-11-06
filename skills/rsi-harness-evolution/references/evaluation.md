# Evaluate improvements without fooling yourself

The objective is better reliable work, not a lower number that conceals omitted work. Define the improvement direction: new functional capability, higher quality, lower cost at maintained quality, faster completion, or better recovery. These require different evidence.

## 1. Make a comparison interpretable

Record baseline/candidate code, loaded implementation, model/version/settings, tool schema, task inputs, initial files/memory, observable environment and required outputs. Keep the robot Policy/data fixed when measuring Harness efficiency. A baseline that is broken or given harder work is not a useful efficiency control.

Select representative workload pairs relevant to the hypothesized mechanism: a normal path, the targeted failure, and an edge/recovery path are useful examples, not a universal fixed suite. Match initial state and evidence access. Snapshot mutable memory separately for each arm unless the experiment explicitly studies continual learning; shared writable memory can teach the candidate the baseline's answers.

Current A3 **phase-two protocol 0.3.0** requires at least three paired Harness workload instances and its stated quality checks. Three is a local minimum, not statistical proof or a rule for every capability task. G follows G acceptance; this skill does not add phase-two trials to it. Read the active protocol if the version changes.

## 2. Assess quality first

Prefer verifiable artifacts, postconditions and fixed tests. Where judgment is needed, define the rubric before seeing candidate results and preserve the evidence used. Blind version labels or swap evaluation order when helpful. Another model is not automatically an independent or reliable judge.

Check completeness, correctness, evidence accessibility, valid recovery and required constraints. Shorter responses that omit an important result, fewer calls that skip validation, or fewer simulation episodes that change the requested work are not same-quality efficiency gains. Do not let an average score compensate for a critical failure. A candidate can improve one dimension and worsen another; report the tradeoff rather than silently redefine success.

Use `passed`, `failed`, `incomplete` and `unknown` distinctly. A tool outage and an Agent's poor recovery can coexist; record both instead of attributing everything to the model or erasing the sample. Do not count only successful runs when reporting costs without also showing all submitted workloads.

## 3. Keep the entire cost account

Report available input/output/cached tokens, tool calls, retries, trial calls, elapsed time, external wait and useful completed work. Include summarization, routing, evaluation and candidate-trial overhead. Distinguish one-time optimization/search cost from recurring per-task execution cost. Unknown usage remains unknown; no model-name-based money estimate.

Separate cold and warm caches, and record backend/provider work versus tool-call attempts. Where uncontrolled load or model randomness matters, interleave or vary order and use repetitions proportionate to the decision. Tiny samples justify per-pair results and cautious decisions, not polished p95 or universal percentage claims. Token savings need not imply latency or financial savings.

Example: a compact tool response cuts model input 40% but causes three extra file fetches and misses a failure record. That candidate fails the quality comparison. A second response view may preserve all required decisions with one optional raw-evidence fetch; evaluate that version rather than concluding all output compression is bad.

## 4. Replay is a filter, not reality

Deterministic fixtures are useful for parsing, routing, checkpoint integrity, invalid inputs and response-loss recovery. Match replayed responses to the request and relevant state. If a candidate asks a different question or submits a different job, blindly giving it the next old response invalidates the comparison.

Use isolated real execution when the changed behavior depends on actual model decisions, environment interaction or restore. Static replay does not measure real network latency or prove long-horizon usefulness. Offline summary-recall fixtures and a real model compaction check complement one another; neither automatically proves better A3 research.

## 5. Avoid selecting on the answers

Keep development diagnostics separate from final evaluation where the task supports that distinction. Repeatedly inspecting a final set to edit the harness makes it development data; do not continue calling it unseen. Split by task family or meaningful variation rather than only paraphrasing the same template. Do not store held-out answers or evaluator internals in candidate memory/skills.

For a small local task, a new representative input and failure case may be enough to reject a bad candidate cheaply. Larger generalization claims require broader evidence. The purpose is an honest decision, not building an evaluation platform before doing useful work.

## 6. Record a decision with its boundaries

Use a concise record: hypothesis → tested loaded versions → paired outcomes and costs → keep/revise/reject/inconclusive → applicability and next action. Preserve failed attempts and counterexamples. A working new capability may be adopted because the current task requires it, without claiming efficiency improvement. An unchanged baseline or removal of an ineffective module is a valid result.

Sources: [HarnessPro](https://arxiv.org/abs/2610.26770v2) separates search signals and final evaluation; [GEPA](https://arxiv.org/abs/2507.19457) illustrates feedback-driven candidate selection; [AIDE](https://arxiv.org/abs/2502.13138) is task-solution search rather than proof of recursive Harness takeover; [agent evaluation guidance](https://www.anthropic.com/research/demystifying-evals-for-ai-agents) distinguishes trajectories and actual outcomes. The recipes above are engineering applications, not reproduced A3 findings.
