# Reliable recovery and actual self-evolution

Editing files, exercising a candidate and transferring the main role are different events. Report which one occurred. A useful component optimization need not claim to be an entire new self-improving system.

## A. Restart loses state or duplicates work

**Diagnose.** Identify durable versus volatile state: message/checkpoint, task ledger, memory, job IDs, produced artifacts, loaded code and pending requests. A file list is not proof that every dependency exists or that the running program writes it correctly.

**Options.** Atomic checkpoint replacement; append-only raw events with stable IDs; small durable operation records; explicit submitted/running/completed/failed/unknown states; integrity checks on essential files; retaining large artifacts through existing references rather than copying them into the message history.

**Cheap experiment.** Interrupt at representative boundaries: before submission, after successful submission but before the response is recorded, during checkpoint writing, and after result completion. Restore using the real entrypoint and check whether it resumes the same work without inventing completion or repeating side effects. An offline fixture can test mechanics; actual integrated resume tests the runtime path.

**Quality gate.** The last valid checkpoint remains usable after a failed write. Known job/artifact identifiers survive. Unknown outcomes remain unknown until reconciled. A restored process uses the intended code/config/inputs, not just a similarly named directory. Required files are still retained after the next normal checkpoint save.

**Counterexample.** A broad retry around the entire loop can endlessly repeat a permanent error. Saving only a textual summary may omit the identifiers needed to query work. Saving the entire transcript does not itself solve context pressure.

## B. Candidate code is edited but not used

**Diagnose.** Check actual entrypoint, imported module path, code/config digests and trace events from the loaded process. Long-running imports, stale processes or inherited prompts can mask a change.

**Options.** One formal isolated trial for a full candidate; startup records of loaded version; behavioral tests that distinguish candidate from baseline; carefully scoped reload only when the architecture supports it. Avoid adding a new hot-loader merely to demonstrate a simple improvement.

**Cheap experiment.** Use a deterministic input on which old and new behavior differ, then run the candidate through its real model/tool interface. Compare the tested and retained implementation digests. Static unit tests support correctness but are not proof the main process adopted the new version.

**Quality gate.** Local wrapper tests, whole-harness trials and actual main ownership are recorded separately. If the final files differ from the evaluated files, recheck the affected behavior rather than inherit the old pass label.

## C. Selection, handoff and rollback

Use the existing A3 trial and decision contract from current execution.md. The current Agent verifies local components itself; ordinary worker creation is denied. The predecessor waits for the one candidate to self-test, and that candidate does not start independent research before adoption. A complete candidate that is adopted follows the existing main-identity transition; a rejected candidate need not be promoted merely to complete an experiment.

A handoff record should include the selected implementation, current confirmed conclusions, their evidence references, unfinished work, active jobs and the intended next action. Verify the actual authoritative main identity before the previous owner exits. Do not create simultaneous owners, assume a trial has already transferred authority, or make every successor re-audit all historical experiments.

Keep a known usable implementation and a compatible state checkpoint. Reverting code does not automatically undo external side effects, database migrations or already-submitted jobs. Test the relevant rollback/reconciliation path. If the candidate is rejected, preserve the reason and return to useful research instead of forcing a successful evolution narrative.

## D. Search over harness designs

Use a single linear candidate chain: one observed problem, one scoped change and one matched comparison. Preserve rejected versions and their evidence for later reference. A rejection returns to the predecessor; adoption transfers ownership to the same candidate. Do not fork concurrent research Agents or start population search.

[HarnessPro](https://arxiv.org/abs/2610.26770v2) is a recent direct reference for trajectory-driven, fixed-weight harness code improvement; its reported results are not an independently reproduced A3 guarantee. [DGM](https://arxiv.org/abs/2505.22954) studies self-modifying coding agents and an archive; its outer exploration machinery is not arbitrarily self-changing. [STOP](https://arxiv.org/abs/2310.02304) studies an optimizer improving its own scaffold. [ADAS](https://arxiv.org/abs/2408.08435) searches agent designs, while [GEPA](https://arxiv.org/abs/2507.19457) uses reflective feedback to optimize textual components. Use the relevant mechanism without collapsing these distinct claims into “RSI proven.”

Changing robot policy code solves the research task; changing the research Agent's own tool/context/control machinery is Harness evolution. A prompt-only improvement can be valuable, but does not by itself demonstrate code self-modification or main-role transfer. Fixed weights do not guarantee a better Agent; the loaded candidate and its outcomes are the evidence.
