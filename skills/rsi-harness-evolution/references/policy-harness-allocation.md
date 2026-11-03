# Choosing Policy research and Harness improvement

This is an initial decision policy for the Agent, not an exhaustive menu, a fixed ratio, a precise ROI formula, or an extra acceptance requirement. Apply it only inside the current task. During G, choose between ways to deliver context management; do not start robot-policy research or require a phase-two efficiency study merely because this reference mentions them.

## Decide from the bottleneck, not the clock

At a meaningful decision point, retrieve relevant outcomes, active jobs and dependencies. Identify what currently limits useful progress and what observation would discriminate competing explanations. Consider a small number of plausible next work packages:

- **Policy information or quality:** when the experiment chain is trustworthy, choose a policy/data/deployment experiment that resolves a consequential uncertainty or improves the current baseline. A focused diagnostic can beat another large training run.
- **Harness leverage:** repeated lost context, redundant work, expensive result handling or a missing research capability can justify dedicated Harness work even when no model job is running. Estimate plausible reuse in the remaining research horizon; validate rather than assume the benefit.
- **Reliability repair:** if observations, job identity, recovery or result attribution are untrustworthy, repair the smallest blocking cause before relying on further experiments. Restore confidence in the affected path; a necessary repair need not pretend to prove an efficiency gain.
- **Wait or gather missing evidence:** when a pending result gates every worthwhile action, waiting is a valid decision. Query at a cadence justified by expected completion and the next decision, not every model turn.

Compare expected research value and information gain with implementation **and validation** cost, risk, dependencies and likely reuse under remaining budget. Prefer grounded estimates and explicit uncertainty to invented scores. A cheap highly informative policy ablation may dominate a speculative framework; a repeated costly recovery defect may dominate another policy variant. Reserve the current protocol's final evaluation and safe-save budget. Do not add a mandatory scoring table, numerical threshold or fixed Policy:Harness allocation.

## Commit to a bounded piece of work

Define the question, change boundary, evidence needed and completion or abandonment condition before execution, using existing experiment memory. Finish a coherent work package rather than trying to fit an entire Harness experiment into a short fine-tuning wait. A package may span several calls or jobs; it is not an open-ended infrastructure rewrite.

Reconsider when a meaningful result arrives, an assumption is falsified, a dependency or resource changes, or cost/risk exceeds the basis of the original choice. A result's arrival is a reason to reassess, not an automatic interrupt: finish a cheap, nearly complete validation when that is still best. Conversely, do not finish sunk-cost work whose justification has disappeared. Keep old plans and observations when revising; no retroactive success criteria.

For example, if a policy run completes during a small recovery fix, record its result and decide whether it changes the fix's value. Do not alternate unfinished work after every status query. If the fix grows into a storage redesign, stop and compare that larger scope against immediate research opportunities.

## Parallelism implements a decision; it does not choose one

When valuable independent questions and available resources permit, actively delegate bounded work rather than keeping every task on the main Agent. Policy alternatives, data preparation or training, and Harness work can overlap when their inputs are ready and their changes are isolated. A running policy job continues with its original inputs while a separate worker tests result parsing; a second training job is useful only if it answers a distinct question and actually receives the required resources. These are examples, not mandatory branches or a work ratio.

Give each worker a concrete question, authorized input and code snapshots, evidence and artifact references to return, and a completion or stop condition. Use existing job/session identities and permissions; do not assume a worker can read another identity's files or inherit its exports. Keep one owner for shared edits and integrate changes against the tested version. Check existing jobs and assignments before dispatch so a timeout, late result or resumed conversation does not duplicate expensive work.

The main Agent remains responsible for dependency tracking, collecting results and making timely keep/reject/next-step decisions. A worker finishing is a reason to close its completed task or record a specific unresolved gap, not to reopen unchanged checks indefinitely. Account for worker calls, failures and integration time when comparing useful throughput. Prefer serial work or waiting when a result gates the next action, resources are unavailable, shared changes conflict, or coordination costs more than the expected gain. Parallelism does not create extra main owners or authorize resource-limit changes.

A short training job does not make a long Harness project worthless, and an idle accelerator does not make an unnecessary policy experiment worthwhile. Conversely, optimizing a remote simulator's scheduling limit is not an Agent-owned Harness change if the limit belongs to Host. Measure the bottleneck and report an external limitation accurately; explore useful permitted work without changing the boundary.

## Learn a better selection policy

Use ordinary decision/result memory to compare the reason for a choice with what happened: Did it resolve uncertainty? Was the supposedly reusable fix actually reused? Did verification consume more than expected? Did a cheap policy probe expose an adapter failure before expensive data collection? Retain evidence and counterexamples, not a long reflection after every call.

Revise the Agent's own scheduling heuristics, task planning or skill copy when repeated observations support it. Treat a changed selection rule as a testable hypothesis, using representative work and the active comparison protocol when claiming gains. Account for the selection mechanism's own overhead. Do not rewrite Host-owned SOUL, goals, acceptance, scientific protocol or resource permissions; a learned heuristic remains subordinate to them. No separate meta-optimizer or new mandatory experiment class is required.

## Failure patterns to challenge

| Tempting rule | Why it can fail | Better starting question |
|---|---|---|
| Always improve Harness while training | Short jobs fragment long changes; worthy Harness work gets postponed when no job runs | Which work package has evidence-backed value now? |
| Always optimize Policy first | Repeated tooling failures invalidate or waste experiments | Is the measurement and execution chain trustworthy? |
| Always remove the largest token block | It may contain decisive evidence; the bottleneck may be external waiting | What quality-preserving change removes measured waste? |
| Build a generic platform for eventual reuse | Validation and maintenance can exceed any remaining reuse | What smallest capability unblocks concrete upcoming work? |
| React to every completion immediately | Context switching leaves both lines unfinished | Does this result change the current package's justification? |
| Follow this menu exhaustively | It suppresses discoveries outside the examples | What unlisted mechanism best explains this actual trace? |

These examples suggest hypotheses, not verified A3 performance gains. Attribute Policy improvements and Harness efficiency separately, keep quality before cost, and retain honest null or rejected outcomes. The active goal and protocol remain the definition of success.
