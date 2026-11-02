# Tool interfaces, observations and repeated calls

A tool is an interaction contract, not just a function name. Improvement may mean fewer tools, clearer schemas or better observations rather than broader execution power. Inspect the actual model-visible definition and the dispatcher together.

## A. Wrong arguments or repeated repair calls

**Diagnose.** Classify failures: schema ambiguity, misleading name, missing precondition, parameter default, inconsistent units/path, or actual backend failure. Capture a small representative set without credentials. Count useful completed operations, not merely syntactically valid calls.

**Options.** Rename ambiguous parameters; expose explicit typed states; validate before side effects; use a targeted task-level tool when a repeated sequence has stable semantics; remove overlapping tools if selection errors dominate. Keep a lower-level escape path only where the existing task and execution contract support it.

**Cheap experiment.** Replay fixed valid, invalid and borderline argument fixtures against the implementation, then check real model choice/correction on held-out tasks. A stricter schema can improve correctness but may prevent a legitimate action; include that case.

**Quality gate.** The backend and visible schema agree. Validation failure must not be reported as performed work. Tool output says what happened, what is uncertain and what action is possible next. Do not expand authority or hide prohibited actions behind a wrapper.

**Counterexample.** A single giant “do everything” tool hides state and makes diagnosis harder. Conversely, a hundred narrow tools can increase prompt and selection overhead. Choose based on observed errors and task structure.

## B. Tool output consumes the context

**Diagnose.** Find repeated full logs, large directory listings, long arrays or redundant metadata. Identify which fields the model actually needed and which rare failure evidence it must not lose.

**Options.** Return structured summaries plus stable artifact references; provide pagination/offsets and filtered views; separate status from full result retrieval; attach machine-readable failure codes and a small relevant excerpt. Keep full raw output accessible instead of deleting it. Allow the model to ask for detail when a summary is insufficient.

**Cheap experiment.** Given the same raw artifacts, compare whether the model reaches the correct conclusion with the compact view. Include a rare error near the omitted section, conflicting metrics and a request for a raw evidence location. Measure total tokens including follow-up fetches.

**Quality gate.** No cherry-picking the successful lines, suppressing errors, rounding away a decision boundary or dropping evidence needed for reproduction. “Fewer tokens” is not a gain if important results become inaccessible.

## C. Errors cause loops or duplicated side effects

**Diagnose.** Distinguish invalid request, permanent denial, missing dependency, temporary availability, rate limit and uncertain outcome. An HTTP timeout does not show whether the remote job or provider request executed.

**Options.** Normalize errors into actionable categories; use bounded backoff for genuinely transient operations; query known state before resubmitting; retain operation identifiers and stable intent keys. A local intent key prevents duplicate backend effects only if the actual backend honors its deduplication semantics; otherwise reconcile the existing operation and preserve uncertainty instead of promising exactly-once execution. Handle failure states in control flow instead of repeating a generic “continue” message.

**Cheap experiment.** Inject response loss after a successful submission, a temporary error followed by success, a permanent denial and a malformed response. Verify only the safe cases retry and uncertainty remains visible. Use local fixtures rather than real private-network or paid failure probes.

**Quality gate.** No duplicate experiment secretly counted twice; no infinite retry; no failed call transformed into a successful empty result. A wrapper must preserve enough backend information to diagnose recoverable failures.

## D. Repeated reads or searches waste time

**Diagnose.** Separate model repeating a decision from genuine cache misses. Inspect whether query/config/version differences are meaningful, and whether stale data is acceptable for this operation.

**Options.** Reuse immutable parsed results; memoize pure transforms; normalize safe equivalent queries; batch independent reads; consult the existing cache before adding a second one. Keep keys tied to source/content/config versions where they affect semantics.

**Cheap experiment.** Cold and warm workloads, concurrent identical requests, invalidation after source changes and failure-result handling. Compare provider/backend work separately from tool attempts. State which cache regime produced the gain.

**Quality gate.** A cached answer is marked with its origin and freshness. No cross-identity or cross-experiment leakage. Missing usage is unknown, not zero; failed upstream calls can still cost resources. Do not infer money saved from token counts without a reliable billing model.

## Sources and applicability

[SWE-agent](https://arxiv.org/abs/2405.15793) supports studying the Agent–computer interface as a performance variable. [Writing tools for agents](https://www.anthropic.com/research/writing-tools-for-agents) offers practical tool/evaluation design; [building effective agents](https://www.anthropic.com/research/building-effective-agents) discusses composable patterns. These do not justify replacing the existing A3 execution boundary or adding tool complexity without a measured need.
