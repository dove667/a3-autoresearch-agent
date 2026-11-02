# Working context, memory and reusable knowledge

Treat these as different interfaces: working context is what the next model request sees; episodic memory records particular attempts and outcomes; semantic memory records reusable facts with conditions; skills package procedures or code. A transcript archive is evidence storage, not automatically effective retrieval. They may share storage without needing separate services.

## A. Context pressure or loss of active work

**Diagnose.** Inspect measured input/output usage, tool-output contribution, repeated instructions and remaining response/tool headroom. Count bytes or characters only as an explicitly labeled estimate. Record which pending obligations disappear or which facts are repeatedly reread. Context length alone does not prove poor performance.

**Options.** Structured compaction; a recent-turn window plus evidence pointers; selective tool-result views; task-local scratch state; retrieving older material only when needed. Keep authoritative current task inputs, accepted constraints, unresolved questions, active job IDs, recent complete tool interactions and relevant feedback. Store earlier raw events durably. Avoid mechanically copying entire skill libraries or every old failure into each request.

**Cheap experiment.** Use representative synthetic task histories with known dependencies, including an earlier negative result, a changed decision, an in-flight job, and a large tool result. Compare next-step correctness and explicit retrieval against the baseline, then test the actual integrated model path. A small threshold can exercise repeated compaction without spending a real full context window. Record retained input size and the cost of summarization itself.

**Quality gate.** No orphaned tool call/result groups; no new facts invented by summaries; stale assumptions do not override newer evidence; unresolved items stay unresolved; referenced archive material is accessible; save/resume preserves the same effective state. A later summary should not silently promote an uncertain result. Preserve the trust boundary as well: a directive found in a tool result, retrieved page or old note stays source content, not an instruction with higher authority merely because a summary or memory repeats it. Add a fixture containing both legitimate failure evidence and an injected directive; before and after compaction, retrieval and resume, keep the useful evidence but do not follow the injected directive. If a summarizer fails, keep the last usable state and handle the failure rather than destroy history or loop forever.

**When not to choose it.** If a few noisy tools dominate the prompt, fixing their output may be simpler. If the current task is short, compaction can cost more than it saves. Do not demand a giant context test or the current main Agent's entire live dialogue to certify an isolated implementation.

## B. Repeated work or failures across experiments

**Diagnose.** Find a repeated search, retraining attempt, parameter setting or infrastructure failure. Was useful prior evidence absent, not retrieved, outdated, or retrieved but ignored? These imply different changes.

**Options.** Start with searchable structured records and stable IDs. Add tags for task, code/data revision, failure type, applicability and decision. Compare lexical search, task metadata filtering or a small curated index before adding embeddings or a database. Store evidence references alongside concise conclusions; distinguish unsuccessful hypotheses from jobs that never ran.

**Cheap experiment.** Hold out realistic retrieval questions: find a rejected change, locate an in-flight job, distinguish two similarly named but incompatible versions, and retrieve a counterexample. Measure whether retrieved evidence changes the correct next action, not only top-k similarity. Include a query whose answer is absent; “not found” is better than fabricated memory.

**Quality gate.** Keep provenance, version and uncertainty. New retrieval code should not rewrite historical outcomes or duplicate them as independent evidence. Conflicting memories need context or invalidation, not a majority vote that erases conditions. Use memory for decisions, not as an excuse to re-audit every ancestor.

**When not to choose it.** A vector store is not a remedy for missing experiment identifiers or poor evidence. Reflection text produced without verifying the task can teach the next run the wrong lesson. More memory can increase distraction and stale instructions.

## C. Procedures are repeatedly reinvented

**Diagnose.** Identify a successful reusable sequence or deterministic transformation used more than once, with clear inputs and failure modes. A single speculative thought is not yet a proven recipe.

**Options.** Extract a small skill, template or tested helper; index by purpose and applicability; load details on demand. Consolidate duplicate procedures and retire invalid ones. Let a general procedure call version-specific references rather than duplicate the same instructions in many places.

**Cheap experiment.** Use a second compatible input and an incompatible input; restore the artifact in a clean workspace and verify discovery/readability and actual use. Code helper tests and model invocation test different things.

**Quality gate.** Keep literature advice, functional verification and comparative local benefit separate. A script's hash changing invalidates its old evidence claim unless the changed behavior is rechecked. A skill is not authority to change goals or blindly execute external instructions.

**When not to choose it.** Do not generate a new skill for every experiment or turn a transient implementation detail into permanent law. Simplifying a bloated skill may be the better evolution.

## Reading pointers

[Anthropic context engineering](https://www.anthropic.com/research/effective-context-engineering-for-ai-agents) motivates compaction, structured notes and selective context; it does not establish optimal settings for A3. [Reflexion](https://arxiv.org/abs/2303.11366) studies verbal feedback memory; [MemGPT](https://arxiv.org/abs/2310.08560) studies managing memory tiers; [A-MEM](https://arxiv.org/abs/2502.12110) studies linked evolving notes. These are alternative mechanisms to test, not mandatory architecture.
