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
  "direction": {"objective": "Improve 2×5 and 2×10 full-evaluation accuracy and success", "metrics": ["Per-task accuracy and native success"], "gaps": []},
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
`job_wait(job_ids=[...])` observes selected jobs even when they
are not yet linked to plan steps. Omit job_ids to use the currently observed active jobs.
By default it waits for a terminal result or notification without waking the model periodically. Optional timeout_seconds bounds the whole wait; zero performs one observation. `idle_wait(seconds=30)`
waits only for operator/input notifications and does not poll jobs. Both modes wake for
input changes and preserve notification acknowledgement through completed tool results. The Agent verifies the step's criterion before recording evidence
and marking it done. The plan tracks Agent claims and observed job states.

The Agent authors the plan through `plan(action="show")` and
`plan(action="update", revision=..., ...)`. The Harness persists it in
the project-root `plan/current.json`, exposed as `runtime_paths.plan`. New runs, resume
and maintenance releases read this shared latest plan. At start, revision and stop,
the runtime preserves an immutable revision/hash snapshot under the trajectory's
`plan-snapshots/`. Each night starts a new run and empty model context; shared workspace, plan, memory and skills carry research progress. Same-day interruption recovery remains an explicit option. The Agent updates its route from new evidence while retaining prior
decisions, job references and results.

The `memory` tool manages shared research experience in `memory/entries.jsonl`.
Entries have stable IDs, revisions, applicability, evidence references and an active/archived
lifecycle. The Host starts an authenticated loopback knowledge worker and a local embedding
process for each research run, then shuts them down together. Model weights remain cached;
no external embedding API receives research notes. Current plan focus supplies a small
retrieved experience view on model requests; complete notes and sources are read on demand.

Retrieval combines normalized keywords, Chinese character terms and local semantic vectors.
Search accepts a natural-language `query`, optional `scope`/`kind`, and `offset`/`limit` pages.
The SQLite index is rebuildable and keyed by model fingerprint and content hash. Writes
check exact and near duplicates; similar findings require an explicit update or a reasoned
separate write. Similarity identifies candidates, while the Agent decides whether their
experimental conditions and conclusions match.

Use `write` for new durable lessons; `read(id=...)` returns the current revision. `update`,
`archive` and `restore` require that revision and preserve prior records. Restore reactivates
an archived note with an explicit reason; obsolete key and expected_sha256 arguments are not accepted. `lifetime=run`
limits a note to `run_ids`, defaulting to the creating run; `run_ids` records associated runs and supplies the allowed run list for run-lifetime notes. Archived
and other-run temporary notes are excluded from normal suggestions and searches. The
current input and permissions govern research; a historical note does not grant permission.

The systems have separate responsibilities: plan describes future research and current work;
memory captures learned findings and failure lessons; skills contain reusable procedures.
`memory(action="sources", id=...)` lists controlled evidence locators. A selected `ref`
can read available workspace text, verified historical bundle text or imported-note provenance.
Job references identify the `job_status` tool; use `job_artifact` for referenced results. Unresolved evidence is reported explicitly.
`recall` searches the current run's original dialogue; it does not search old bundle files.

Use `recall(keywords=[...])` for current-run observations. Optional `recent_rounds`
restricts the search to recent decisions. Evidence mode excludes tool arguments
and retrieval responses; raw mode also searches arguments. Both exclude recall's
own queries and responses to avoid retrieval echoes.

The history log preserves model-visible messages, including bounded output views.
Recall follows runtime references into verified original tool JSON and UTF-8
artifacts, so metrics omitted from previews remain searchable. Search streams
files and returns bounded `items` with `rounds_ago`, `snippet`, matched terms and
`source`: path, SHA256, character offset and read limit. Paths are relative to
`runtime_paths.state`. Missing or corrupt sources appear in `warnings`. Hash
verification and scanning use the same file descriptor and reject symlinks.

## Prepare and submit GPU jobs

Call `job_prepare(command=[...], gpu=true, attachments=[...], input_exports=[...])`
with the production command once. Attachments accept regular workspace UTF-8 files,
including scripts and experiment configurations; each item provides a path and optional
remote destination, and the runtime computes SHA256. Binary data and weights use immutable
exports. The runtime saves the exact request and starts its CPU check.

The real entry point must handle `A3_RESEARCH_PREFLIGHT=1`: load the checkpoint and
actual dataset inputs, import dependencies, and exercise a minimal forward or data read.
Before exiting zero, write `a3-preflight.json` in the job workspace with
`{"schema":1,"status":"passed"}`. Preserve loading errors and avoid production outputs
in this branch. CUDA-specific behavior is checked by the subsequent GPU execution.

Preparation returns `prepared_id` and `preflight_job_id`. Wait with
`job_wait(job_ids=[preflight_job_id])`, then call `job_submit(prepared_id=...)`.
The runtime checks completion, the report and unchanged attachment hashes, then submits
its saved request. Modified inputs require a new preparation. CPU work uses
`job_submit(command=[...], attachments=[...])` directly. No second model-facing GPU
submission or raw HTTP entry is exposed. Saved receipts fence uncertain sends and avoid
repeating a production request; inspect `job_status` or `job_list` to reconcile uncertainty.

## Token budgets and full-transcript summaries

Before each decision, the Harness reads authenticated `GET /context/config` and
estimates the entire request with the bundled offline `o200k_base` tokenizer.
The count includes messages, tool definitions, operational state, injected inputs,
feedback and provider framing. Deployers configure the actual model windows and
compare estimates with returned usage.

`A3_CONTEXT_TARGET_TOKENS` is the target for the complete post-compaction decision
input. For a 32k target, the Harness deducts fixed instructions, tools, the current
operational frame, the summary wrapper and the newest complete tool batch. The
remaining tokens fund the summary. A large fresh observation can raise this soft
target within the hard decision budget, preserving useful summary space. Only
the model window and framing/output reserves are hard admission limits.
Its output allocation follows the remaining
budget and the summary model's actual window; there is no separate fixed 1000- or
4096-token cap, byte ceiling or fixed short-summary length.

The summary request contains the complete readable current context in one pass,
followed by a saved `CURRENT_STATE_REFRESH` frame when available. Its source
snapshot includes both, so every source index remains inspectable. Previous
summaries remain part of current context; compression does not reread the entire
lifetime history. Large outputs contribute semantic facts and partial previews;
their complete originals are available through recall and read.
The Host checks its input and requested output together against the configured
summary window. A complete successful response is required before compacted state
is published. Temporary transport failures use persisted exponential backoff,
capped at 60 seconds between attempts, with no total time or attempt cutoff.
Each summary request has a 960-second transport timeout; a timeout schedules
another attempt rather than ending the run. Invalid summary content has three
repair attempts, counted separately from transport failures.

When those repair attempts are exhausted, the Harness does not stop the run and does
not publish an archive pointer. It rebuilds a **degraded handoff** without a model:
each entry is a verbatim excerpt of a cited message, taken from the latest state
refresh (objective, ready-step tasks, active jobs, review-due steps) and the newest
tool results, so schema, quotation and numeric validation still hold. The compaction
record marks `summary_mode="degraded"` with the reason, and the research ledger keeps
one line per compaction. Two consecutive failures on the same model route open a
30-minute breaker (`conversation/summary-breaker.json`) that skips further summary
requests, so a weak summarizer cannot spend three full-transcript requests per
compaction; one successful summary clears it. Damaged saved-summary state is a
different case: it still raises and stays visible rather than being overwritten.

There is no 300-second compaction exit deadline. While history fits the hard
budget, a pending background summary can coexist with normal decisions. At the
hard budget, the decision loop waits in the same process until a valid summary
can be admitted. Authentication, source-integrity or repeated validation errors
enter a visible blocked state without generating research decisions. Source and
budget contracts are rechecked; repaired configuration can enable another attempt.
The runtime preserves its existing context and journal throughout this wait.

`conversation/context-failure.json` reports `waiting` or `blocked`, the reason,
start/update time and next recheck interval. A successful decision gate clears
this indicator. Waits check Host identity and input revisions, use interruptible
short sleeps, and remain responsive to the Host's native stop/suspend signals.
They perform no research tool calls. Explicit operator stops, invalid initial
recovery state and failures that prevent safe tool-result persistence retain
their separate shutdown behavior.

Model-visible messages remain in `conversation/history.jsonl`; complete large
tool results remain in verified content-addressed files with bounded facts and previews;
remote text artifacts use character pages with source hashes. This result transport
keeps the request bounded while preserving access to the actual observations.
The newest complete tool batch stays paired and visible. If it cannot fit within
the hard budget with a useful summary, tool text can be represented by semantic
facts and verified original references before another summary is requested.

### Weak-model deployment profile

A weaker main model is limited by the fixpoint of model quality and system design, not
by the context window. These settings matter more than the window size:

- Pin the summary role to a stronger model (`A3_SUMMARY_MODEL_NAME`), so compaction
  quality does not depend on the research model. The degraded handoff covers the rest.
- Keep `trigger_tokens` well below the hard budget: a weaker model plans better from a
  smaller, refreshed working set than from a 400k history. 100k–150k with a 32k target
  is a reasonable starting point; `A3_CONTEXT_TARGET_TOKENS` may stay at 32k.
- Optional tool families are advertised only when the deployment serves them, so the
  tool list does not carry dead entries. `capabilities.web` comes from the Host web
  service when the Harness reads `/context/config`; hidden tools stay dispatchable.
- `prompt/SOUL.md` stays short (about 700 tokens) and method detail lives in skills,
  which keeps per-request overhead near 10k tokens instead of 15k.
- Watch `research-ledger.jsonl` and the `model_drift` trajectory events: they show
  repeated rejected calls, prose-only responses and degraded compactions, which are the
  first signals that the model is being asked for more than it can do.

The template currently provides explicit model windows, a trigger and a 32k target.
Set them in the Host's `.env` or `A3_RSI_ENV_FILE` for the selected model. The Host
loads budgets at startup, and the Harness reads the active contract before decisions.
Published code is changed through the stopped-run release procedure; existing
historical source and checkpoints retain their original versions.

## Source-bound research handoff

The summary model returns a versioned JSON handoff with seven sections: corrected
goals, experimental results and conditions, failures and rejected approaches,
active jobs and uncertain submissions, unresolved work, next actions and sources.
Input messages carry explicit one-based source labels for direct citation.
Every handoff item includes its narrative, a source message index and an exact
source quotation. Empty sections are explicit. The runtime validates schema,
source bounds, quotations, numeric claims and job IDs before rendering readable
working memory. Both generated and rendered size must fit the remaining budget.
Validation failures supply repair feedback for the next attempt; feedback cannot
be cited as evidence. Completed documents and validation reports are checksummed
and revalidated on recovery.

These checks catch fabricated quotations and changed values such as accuracy
0.42 becoming 0.82. They cannot establish every natural-language inference or
prove that a summary covered every useful observation. The Agent checks primary
results and persists significant conclusions in plan reviews and memory.

## Fresh facts across repeated compression

Each decision includes a fresh source frame, outside compressed history. It
carries the current plan, recent route decisions, active job observations, recent
exact tool receipts and retrieved memory suggestions. The Agent reconciles old
pending claims against this frame and checks existing job IDs before resubmission.

`conversation/research-receipts.json` indexes up to 96 recent significant tool
observations, each bound to its complete original output by SHA256. It is recorded
after the raw tool journal and before bounded result publication; recovery indexes
the saved result without executing the tool again. The decision frame includes at
most six recent receipts, four reviews, four closed-route conclusions and 32 active
job observations, with byte bounds and explicit omission counts. Long conclusions
are explicitly partial. Read the index or use recall for older output sources,
and plan show for full decisions.

The index records program observations. Plan manages research decisions, memory
preserves learned findings, and skills preserve reusable methods. Fresh runs
rebuild guidance from shared plan and memory; their run-local index starts empty.

## Deliver observations before compacting them

The Harness prepares context **before** the next model decision. The final complete
tool-call/result batch is therefore a new observation, not already-read history.
If that batch fits with working-memory headroom, it remains intact while older
history is archived or summarized. A lower compression target does not justify
moving that new observation into the summary. Call/result pairing is preserved.

A result exceeding the 12,000-byte inline envelope is retained losslessly in a
content-addressed archive. Its bounded view prioritizes job/preparation/export
IDs, status, exit code, errors, metrics and source references before head/tail
preview text. JSON artifacts expose decoded metrics with their outer receipt.
Selection is bounded by depth, visit count and 48 facts; omission counts and
`facts_complete=false` make partial delivery explicit. Long errors have explicitly
named previews and original lengths. Credential-shaped fields are excluded from
fact extraction. Read needed pages or search originals with recall. No separate
spill mechanism is used.

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
a checksummed completed summary or its pending retry time. Transient retry counts
and content-repair counts are independent. Retriable summary attempts remain source-bound and
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

Offline tests cover semantic output views, source-linked handoffs, role/auth boundaries, injection reserves,
single-pass full-input admission, durable no-replay behavior and storage/journal failure.
Real validation must separately check actual response models/options, semantic
corrections/unknowns, source reads, normal tool work after recovery, and reference
reachability. Neither a process exit code nor a mock response proves those facts.


## Automatic observations and bounded analysis

The Host sets `A3_STATE_ROOT` to `trajectories/<run>/conversation/`. The fixed
runtime stores its resume/continuation manifests, outcome, checkpoint, tool
journal, history, trajectory, operational ledger, compaction transactions, prepared-job
receipts and failure records there. Research code stays in `workspace/scripts/<model-or-method>/<optional-series>/<purpose>/`.
Experiment configurations, results, figures, videos and analysis stay in
`workspace/experiments/<date-name>/`, with job/export IDs and source hashes. Skills use the single root
`skills/` library.

The Host sets `A3_PLAN_FILE` to the project-root `plan/current.json`. Storage uses version2
and `direction.objective` is its sole objective field. The `plan`
tool maintains three levels: `direction` contains the long-term objective, metric
list and capability gaps; stable-ID `routes` contain research questions,
hypotheses, experiments, decision rules and priorities; `steps` contain route
links, completion criteria, dependencies, job IDs and evidence. Priorities range
from 0 to 100, with higher values first.

Steps carry `depends_on`; the plan validates that dependencies identify plan
steps and contain no cycle. `show` returns steps in dependency order, with
priority breaking ties among steps that are ready together; this order is
computed by the plan view, not inferred by the caller.

Use `show` before `update`, then pass the current revision and patch only changed
fields. Updates use a file lock and atomic publication; stale revisions require a
fresh read and merge. After inspecting a significant result, use `review` with the
route `id`, current `revision`, `conclusion`, `evidence` and a decision of
`continue`, `pause` or `complete`. Reviews persist across runs and record the last
observed terminal statuses of associated jobs. Each model request includes a
compact planning view; `review_due` highlights observed terminal jobs not yet
covered by a review. The Agent interprets results and chooses scientific actions.

New runs read the same plan, reconcile existing jobs and reconsider old blockers
before choosing their next experiment. Ordinary file tools do not modify the plan.
Each trajectory retains immutable revision snapshots under `plan-snapshots/`;
old `conversation/research-plan.json` files remain historical material.

Plan is the only Agent-maintained task ledger. `conversation/operational-state.json`
contains automatic job and read observations, not another editable task list.
Use `research_status(status="continue", reason=...)` to report whole-run progress.
An external blocker uses `status="blocked"`, `reason`, `kind`, `required_action` and
workspace `evidence` paths. The runtime infers the need for operator action and saves
`run-outcome.json`; resume and continuation manifests are generated automatically.

`context-history/` paths are logical source references relative to
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

Automatic read and job observations are attached as a bounded ephemeral frame on each
normal decision, outside rolling summaries and the saved model history. Its cost is
reserved before compression. The exact frame is archived with source references;
resume/continuation lists are generated from workspace research files. A corrupt sidecar
fails rather than silently resetting observations. Task decisions and their evidence are
maintained in the shared plan; there is no second model-authored operational task list.

Resume and maintenance releases preserve the current conversation state. Future
sessions share research files in `workspace/`, the root skill library and active
long-term experience in `memory/entries.jsonl`. Operational records remain with
the run that produced them.

The current plan retains the latest 20 review receipts and a job-status review index. Earlier complete revisions remain in each run’s immutable plan snapshots. Automatic context shows concise direction and route fields; `plan(action="show")` returns full detail.


## Skill discovery and progressive disclosure

The same authenticated knowledge service provides `/memory` and `/skills/search` using one
Qwen embedding process. Memory records and skill packages retain separate storage and APIs.
Shared text normalization and vector caching live in `harness/semantic_index.py`; memory
ranking remains in `harness/memory/retrieval.py`, and skill discovery in `harness/skill_search.py`.

`skills(action="list", query=...)` searches purpose metadata and private body-section vectors.
Only ID, version, status, a short description and match reason are delivered. Resources are not
automatically read or indexed. Read a selected `SKILL.md`, then list and read only needed
resources. Empty-query listing needs no embedding inference. List pages accept 1–24 entries;
body and resource reads accept 1–8000 characters.

Skill vectors are cached separately under `.runtime/skill-index/`. Each query snapshots the
current package under its library lock; archived packages are omitted by default. Content
hashes refresh changed text while identical vectors are reused, so edits and archive operations
are reflected on the next query. An unavailable production service returns an explicit error.

For local corrections, `edit` and `edit_resource` replace exact text after checking the current
body/resource SHA256. A match must be unique unless `replace_all=true` is explicit. Markdown
body edits preserve managed YAML metadata. Binary resources, stale hashes and missing or
ambiguous matches are rejected. Complete old packages are retained before each versioned
transaction; unchanged edits do not increment the version. Use `save` to create a package or
change its description, keywords or complete body.


## Model-visible tool results

The model uses named high-level tools for jobs, exports, evaluation comparison, visual
inspection and session status. HTTP routing, credentials and the Host Gateway remain
internal. Every model-visible result has `ok=true` or `ok=false`; failures include an
`error` and a correction `hint`. List operations expose a continuation cursor and reads
expose the source hash and character page. Replacing an existing workspace file requires
its current expected_sha256; use `edit` for a small change.
