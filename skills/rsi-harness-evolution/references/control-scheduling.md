# Planning, scheduling and useful parallelism

Optimize the actual critical path, not activity count. A plan is an aid to choosing actions; it is not evidence that those actions happened.

## A. Long plans, drift or unproductive reflection

**Diagnose.** Compare declared objectives with actual calls and artifacts. Look for repeated planning without execution, unchanged hypotheses after contrary evidence, too-early completion, or endless work after completion. Distinguish missing information from a control-flow mistake.

**Options.** A short task ledger with dependencies and evidence links; replanning when observations contradict assumptions; explicit state transitions for stable steps; a small evaluator for verifiable postconditions; removing repetitive self-reflection prompts. Keep open-ended research decisions open rather than encoding every possible path.

**Cheap experiment.** Representative workloads include a straightforward success, a misleading intermediate success and a recoverable failure. Compare an unmodified baseline with one planning/control change. Check both final artifacts and behavior after the stopping condition, not whether the model wrote a convincing plan.

**Quality gate.** Completion follows the current task's real outcomes. A gate must not reject a valid alternative method merely because it differs from an example. Preserve unfinished status and evidence when blocked; do not treat exhaustion as success. Repeated “continue” messages are not a substitute for diagnosing missing observations or a broken terminal protocol.

**Counterexample.** A planner–critic–executor trio can triple calls while sharing the same mistaken assumption. A deterministic parsing task may need only a tested helper. For a novel research problem, a rigid workflow can suppress useful exploration.

## B. Idle time or duplicated expensive work

**Diagnose.** Build a small timeline of submit, queue, start, complete, observe and next decision. Separate simulator/data/remote-service time from time spent deciding and polling. Check whether a job already exists before concluding that nothing is running.

**Options.** Track job IDs and dependencies; collect status in batches; adapt polling to expected job duration; perform independent analysis while a job runs; reuse a compatible baseline or immutable dataset; stop an obsolete candidate only when its state and current task permit it. A slow environment is not automatically a poor harness.

**Cheap experiment.** Use deterministic fake jobs with different durations, an interrupted response and an already-completed job. Verify bounded polling, no duplicate submissions and prompt use of newly available results. Later measure real workloads when the task permits them; a simulated clock does not establish real latency gains.

**Quality gate.** No dependent work runs on missing inputs; queue time is not relabeled as compute savings; completed results remain reachable. A shorter development subset is a legitimate research choice but not a comparable Harness gain over a baseline required to do more work.

**Counterexample.** Busy polling may make a dashboard look responsive while wasting calls. Caching running-job state too long can cause unnecessary waiting. Removing all waits can create duplicate work rather than speed.

## C. Independent remote jobs and trial boundaries

**Diagnose.** Identify jobs with fixed inputs and independent outputs. Check data dependencies, model/data snapshots and available resources before submission.

**Options.** Submit ready independent remote jobs first, then collect their results with bounded status polling. Collection and evaluation episodes use `/run/a3/episode_executor.py` inside the authorized job. The same research Agent writes scripts, fixes ordinary failures and interprets results.

**Experiment.** Match useful work, scientific parameters and resource allocation; count all calls, failures and integration time. Reuse valid completed results. Use separate throughput probes rather than repeating scientific episodes.

**Quality gate.** Keep one research Agent and one formal trial candidate at most. The predecessor waits for the candidate's self-test; the candidate waits for adoption before independent research. Reject or adopt that same candidate, preserving evidence, memory, permissions and active-job references.

## D. Routing and model-call organization

Routing can select a deterministic helper, a retrieval path, a specialized prompt. Test whether the route improves outcomes over the default rather than adding an extra classification call to every task. Include ambiguous and out-of-distribution cases with a fallback.

A model or reasoning-setting change is a separate experimental variable, not a hidden Harness improvement. Use only the model options actually available in the current run. Do not copy another provider's caching or response-state parameters into the A3 gateway without checking support. Keeping stable prefixes and tool ordering may be worth testing, but provider caching is not the same as a local semantic-result cache.

Sources: [Building effective agents](https://www.anthropic.com/research/building-effective-agents), and [mini-SWE-agent](https://mini-swe-agent.com/latest/). The former offers design experience; the latter is a useful simple baseline.
