# Context recovery and model roles

The Harness in `src/a3_research_agent/harness/` supplies context management,
research planning, durable memory and built-in tools. The Host runs a fixed
published Git commit in a native process, separate from the writable research
workspace. Maintenance releases preserve the run's checkpoint and research files.

`a3_research_agent.harness_payload.build_harness_payload()` exports the
complete declared Python payload, including its token estimator, for packaging
and validation. Tool implementations belong in that declared source tree. The
fixed runtime command is `python -P -m a3_research_agent.harness.agent --resume`.
Publication and in-place updates follow the [operator workflow](../guides/maintenance.md).

## Research plans and durable notes

The Agent uses `plan(action="show")` to read its current revision and
`plan(action="update", revision=..., steps=[...])` to patch steps by ID. New steps
carry a task and a concrete completion criterion. Dependencies refer to step IDs.
The Agent records observed progress, relevant Controller job IDs and evidence
references. Closed steps can be archived when no current step depends on them.
Revisions preserve the previous plan and record reasons for route changes.

```json
{
  "action": "update",
  "revision": 0,
  "goal": "Train ACT on a verified expanded dataset",
  "steps": [
    {
      "id": "collect",
      "task": "Complete and audit the 2×10 DAgger batches",
      "criterion": "Manifest contains 1,000 unique eligible trajectories"
    },
    {
      "id": "train",
      "task": "Start ACT with the expanded manifest",
      "criterion": "Training report identifies the manifest and checkpoint",
      "depends_on": ["collect"]
    }
  ]
}
```

The decision context includes a bounded plan view with ready steps and the last
observed job status. An active step with a queued or running job can wait. When
independent work is ready, the next decision frame includes those step IDs.
`wait` observes submitted jobs even when the Agent has not yet linked them to a
plan step. Program-level waiting handles queueing and running jobs without
model polling. The Agent verifies the step's criterion before recording evidence
and marking it done. The plan tracks Agent claims and observed job states.

The Agent authors the plan through `plan(action="show")` and
`plan(action="update", revision=..., ...)`. The Harness persists it in
the project-root `plan.json`, exposed as `runtime_paths.plan`. New runs, resume
and maintenance releases read this shared latest plan. At start, revision and stop,
the runtime preserves an immutable revision/hash snapshot under the trajectory's
`plan-snapshots/`. Each night starts a new run and empty model context; shared workspace, plan, memory and skills carry research progress. Same-day interruption recovery remains an explicit option. The Agent updates its route from new evidence while retaining prior
decisions, job references and results.

The `memory` tool manages active long-term experience in the shared project-level
`memory/entries.jsonl`. Use `memory(action="search", key=...)` to look up prior conclusions, failures
and verified entry points. Add `kind` or `scope` when a narrower search is needed,
then read the selected note by ID. Notes can carry applicability in `scope`,
source IDs or paths in `refs`, and an old note ID in `supersedes`. Search excludes
superseded notes, which remain readable by ID. An empty search returns recent
current notes with their source references. Existing free-text notes stay
readable and searchable. The Agent chooses which findings to record and checks
their applicability before reuse.

Use `recall` for original observations retained in the current run's `conversation/history.jsonl`. Without `n`/`m`, it searches the available
original history; an explicit round window retains its existing meaning. Search
streams rounds and keeps bounded ranked snippets. Its default evidence mode ranks matching
snippets and excludes tool arguments and prior retrieval responses. Use
`mode="raw"` for historical tool traffic. Both modes point to the original round,
and the complete source history remains on disk.

## Preflight for long GPU jobs

Call `preflight(action="check", job=...)` with the complete production
`POST /jobs` request, including `gpu: true`. The Harness sends the same command,
inline source, hashed workspace references and read-only exports to a CPU job
with `A3_RESEARCH_PREFLIGHT=1`. Implement this mode in the real entry point.
Load and check the checkpoint, import the adapter and its dependencies, and
exercise a minimal forward pass or a real dataset read as applicable. Before
exiting zero, write `a3-preflight.json` in the job workspace with
`{"schema":1,"status":"passed"}`. Keep this branch out of the production output
and preserve the error when loading fails.

After completion, call `preflight(action="submit", preflight_id=..., job=...)`
with the same production request. This tool requires a successful Controller
exit and a readable report. Any request change requires a new check. Pin assets
to immutable exports or verify the expected content digests in the entry point.
The CPU check can surface import, load, and input errors. CUDA-specific behavior
still requires a GPU pilot. Existing ordinary job submissions keep their API.
Same-run resume preserves receipts. If production submission is uncertain, use
the registered job ID or check the job list before another attempt.

## Token budgets and full-transcript summaries

Before each decision, the Harness reads authenticated `GET /context/config` and
estimates the entire request with the bundled offline `o200k_base` tokenizer.
The count includes messages, tool definitions, operational state, injected inputs,
feedback and provider framing. Deployers configure the actual model windows and
compare estimates with returned usage.

`A3_CONTEXT_TARGET_TOKENS` is the target for the complete post-compaction decision
input. For a 32k target, the Harness deducts fixed instructions, tools, the current
operational frame, the summary wrapper and the newest complete tool batch. The
remaining tokens fund the summary. Its output allocation follows this remaining
budget and the summary model's actual window; there is no separate fixed 1000- or
4096-token cap, byte ceiling or fixed short-summary length.

The summary request contains the complete readable source transcript in one pass.
The Host checks its input and requested output together against the configured
summary window. A complete successful response is required before compacted state
is published. Temporary failures and incomplete responses use the bounded summary
retry protocol, preserving the source snapshot and each attempt. Exhausted failures
keep the checkpoint recoverable and block the decision that requires compaction.
The runtime does not generate a no-summary archive fallback.

Original messages and tool results remain in `conversation/history.jsonl` and verified source
snapshots. Large tool results are separately stored losslessly with bounded previews;
remote text artifacts use character pages with source hashes. This result transport
keeps the request bounded while preserving access to the actual observations.
The newest complete tool batch stays paired and visible; its size can make the soft
target infeasible, in which case the hard model-window admission still applies.

The template currently provides explicit model windows, a trigger and a 32k target.
Set them in the Host's `.env` or `A3_RSI_ENV_FILE` for the selected model. The Host
loads budgets at startup, and the Harness reads the active contract before decisions.
Published code is changed through the stopped-run release procedure; existing
historical source and checkpoints retain their original versions.

## Deliver observations before compacting them

The Harness prepares context **before** the next model decision. The final complete
tool-call/result batch is therefore a new observation, not already-read history.
If that batch fits with working-memory headroom, it remains intact while older
history is archived or summarized. A lower compression target does not justify
moving that new observation into the summary. Call/result pairing is preserved.

A result too large to fit is retained losslessly in a content-addressed archive,
with a bounded, explicitly partial head/tail preview and its original character
count. The preview is not a complete inspection or a new pending research task.
Retrieve only needed details in small pages. This avoids replacing a requested
observation with only another archive pointer. Archiving records storage state;
it does not prove that a model understood the output or completed a check.

Summary preparation excludes only opaque `encrypted_content` from reasoning
items. The full original snapshot retains it, recent provider-state items remain
with their fresh tool batch, and readable message content is unchanged. Ciphertext
is not semantic evidence that a different summary model can interpret.

Checkpoint publication also preserves existing handoff metadata, including declared
remote export references. Invalid existing manifests fail rather than silently
resetting their state. These measures do not automatically close experiments or
make keep/reject decisions: the Agent must still record those decisions from the
actual evidence. Stale research files and a semantic-summary error remain separate
possible causes; the transport fix does not certify autonomous long-run convergence.
The Agent records experiment decisions and job references explicitly; saving a
checkpoint preserves the current context and tool state.

## Fixed roles and narrow summary operation

`MODEL_NAME`, `MODEL_CLASS` and `A3_MODEL_REASONING_EFFORT` remain the primary model
configuration. Optional `A3_WORKER_MODEL_NAME` and `A3_SUMMARY_MODEL_NAME`, with their
respective `A3_WORKER_REASONING_EFFORT` and `A3_SUMMARY_REASONING_EFFORT`, configure
fixed helper and summary routes. Absent overrides, these routes use the primary model.
All these settings are Host-owned; research processes cannot supply upstream URLs, keys or
arbitrary role-model mappings. The supported service-tier override remains
`A3_MODEL_SERVICE_TIER=default` (Standard).

| Session or operation | Route and authoritative contract |
| --- | --- |
| Current main | Primary model |
| `POST /v1/summary` | Summary override, otherwise primary; data transformation only |

A test assistant and a model under test are different roles. Testing a future main's
behavior with a helper model would not establish behavior of the deployed main.
Maintainers validate changes to the main Harness during a maintenance window. Ordinary `/sessions` creation and `candidate_validation` helpers are denied to Agent tokens. Role and requested/actual model options are audited.

`POST /v1/summary` accepts the full `transcript`, a relative
`context-history/snapshot-<SHA256>.json` source reference and the requested output
allocation in `max_output_tokens`. It cannot receive client
instructions, tools, model selection or credentials. The Host supplies concise
source, uncertainty and correction instructions for this history transformation. The output grants no authority and never executes tools. The current
SOUL continues to govern normal research decisions.

Summary metadata binds operator-feedback IDs and hashes. It does not copy every
full notice into the summary request and does not mark a reference as full delivery.
Full notices remain durably stored on the Host and are still delivered unchanged
to that owner's normal decisions, including after compression and resume. They
remain attached to the same research run through maintenance releases.

Set `A3_MODEL_CONTEXT_TOKENS` and, for alternate models,
`A3_WORKER_CONTEXT_TOKENS` / `A3_SUMMARY_CONTEXT_TOKENS` to their supported windows.
`A3_CONTEXT_TRIGGER_TOKENS` selects when to start compression;
`A3_CONTEXT_TARGET_TOKENS` budgets the complete compacted decision input, including
Host instructions, operator feedback and tools. The context endpoint reports this
same total target without adding another reserve to it.
`A3_SUMMARY_INPUT_TOKENS` was removed and is rejected if present.

## Durable failure behavior and deployment

Each complete-transcript request is recorded **before** dispatch. A restart reuses
a checksummed completed summary. Retriable summary attempts remain source-bound and
are recorded separately; this operation only transforms supplied text. Tool side
effects retain their separate no-replay journal. Corrupt request records fail
explicitly and original snapshots remain intact.
The new `transcript`/`source` protocol rejects old fields and requires a matching
Host and Harness update. Local source changes do not update live processes.

Resume and continuation manifests are published before checkpoints that may cite
new archives. File and parent-directory writes are synchronized. The tool journal
keeps an idle `null` file; interrupted operations remain unknown and are not blindly
submitted again. These measures are not a multi-file power-loss transaction or a
promise of perfect semantic summaries. Persistent provider/storage/budget failures
can still require an explicit operator repair.

The runtime generates `resume.json` and `continuation.json` in the current
conversation state. Their `files` lists identify workspace research files. The
ordinary entry remains `python -m a3_research_agent.harness.agent --resume`.
Continue research through `--resume-from` on the original run; retain historical
evaluations with their original model, dataset and settings. For a paused process, editing disk
modules does not reload already imported Python code. Any later authorized update
must preserve the checkpoint/journal and deliberately restart only the owned
process through the existing stopped-main recovery path; ordinary unpause alone
is insufficient. Record the intervention in resume feedback.

Offline tests cover archive-only behavior, role/auth boundaries, injection reserves,
single-pass full-input admission, durable no-replay behavior and storage/journal failure.
Real validation must separately check actual response models/options, semantic
corrections/unknowns, source reads, normal tool work after recovery, and reference
reachability. Neither a process exit code nor a mock response proves those facts.


## Durable operational memory and bounded analysis

The Host sets `A3_STATE_ROOT` to `trajectories/<run>/conversation/`. The fixed
runtime stores its resume/continuation manifests, outcome, checkpoint, tool
journal, history, trajectory, operational ledger, compaction transactions, preflight
receipts and failure records there. Research code stays in `workspace/scripts/<model-or-method>/<optional-series>/<purpose>/`.
Experiment configurations, results, figures, videos and analysis stay in
`workspace/experiments/<date-name>/`, with job/export IDs and source hashes. Skills use the single root
`skills/` library.

The Host sets `A3_PLAN_FILE` to the project-root `plan.json`; the `plan` tool
reads and updates this shared research plan. Ordinary file tools do not modify it.
Each trajectory retains its historical snapshots under `plan-snapshots/`; old
`conversation/research-plan.json` files remain historical material. Read the latest
plan and reconcile its existing jobs before further submissions.

The `progress` tool maintains run metadata. The Agent records its
research status with `progress(action="outcome", status="continue"|"blocked",
reason=..., external_blocker=...)`. The runtime saves `run-outcome.json` and generates
the resume and continuation file lists; the Agent works through the corresponding
tools.

`context-history/` and `.autorun-spill/` are logical source references relative to
`runtime_paths.state`. Their contents and hashes remain unchanged. Ordinary file
tools can read/list/search the current conversation directory but cannot write or
edit it. Skills and long-term memory use their dedicated tools.

The runtime returns normal text reads as bounded character-page objects:
`path`, whole-file `sha256`, `offset`, `end`, `total`, `eof`, and `content`.
Offsets refer to decoded characters, not UTF-8 bytes. Pages are bounded by actual
serialized call-output size. Hashing and character extraction stream the source
without loading the full file into memory; hashing still reads the full source.
Optional top-level JSON field selection rejects sources above 1 MiB with a paging
notice, before parsing the object. `conversation/operational-state.json` records retrieved ranges separately from exact
page payloads included in a successful completed model request. Neither proves
understanding. Sparse coverage is bounded with an explicit incompleteness marker;
gaps are never merged into unread coverage. Views and receipts have finite caps.

The executed raw result is journaled before optional receipt/preview work. An
archival failure is a persistence failure, not a claim that the operation failed.
Recovery republishes saved results without executing the tool again, reconciling
read receipts idempotently. If the outcome was never durably recorded, the journal
keeps an explicit unknown outcome requiring inspection. `conversation/loop-failure.json` records
the failure stage and safe context-budget diagnostics; an exit code alone is not
completion evidence. Atomic writers use unique temporary files and verify reused
content-addressed sources.

These checks cover directly referenced control sources and decoded dependencies.
They do not continuously traverse every historical citation or establish semantic
truth, fault-free upstream services or unlimited disk space.

The `progress` tool records explicit Agent-authored open/waiting/closed items and
workspace-relative evidence references; research decisions remain Agent-authored.
Missing/large/changed evidence is uncertain, not automatically passed or reopened.
Current operational facts are attached as a bounded ephemeral frame on each normal
decision, outside rolling summaries and the saved model history. Its estimated
cost is reserved before compression. The exact frame is archived with its source
references; resume/continuation lists are generated from workspace research files.
A corrupt sidecar fails closed rather than silently resetting it. One previous sidecar and separate
progress-item archives bound redundant storage while preserving task changes.

Resume and maintenance releases preserve the current conversation state. Future
sessions share research files in `workspace/`, the root skill library and active
long-term experience in `memory/entries.jsonl`. Operational records remain with
the run that produced them.
