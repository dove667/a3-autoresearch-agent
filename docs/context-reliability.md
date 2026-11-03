# Context recovery and model roles

The optional overlay in `runtime/harness_recovery/` adapts a compatible retained
Harness without changing its historical ready version. Copy `agent.py`, `context_manager.py`, `memory_state.py` and `analyzer_client.py`, plus the shared `runtime/local/token_count.py` as a flat
`token_count.py`, into an explicitly derived package. Preserve its compatible
`web_tools.py`, `skill_tools.py`, memory and evidence. Record source/version hashes
and the intervention; an old acceptance record does not certify new code.

## Token budgets and archive-only recovery

The overlay asks authenticated `GET /context/config` for current nonsecret reserves
before a normal decision. With verified model windows configured, it uses the
already bundled offline `o200k_base` codec, not UTF-8 bytes divided by a constant.
Counts of serialized inputs, tool schemas and message framing are **estimates**;
they are not provider-exact billing or proof that a model uses that exact codec.
The Host also reserves provider framing and output space, and enforces input
headroom against the declared model window. Verify the declarations against the
actual model catalog and compare estimates with real returned usage.

The default trigger is 64,000 estimated input tokens and target is 32,000, including
injected reserves. The lower target leaves space for useful normal work before the
next compression. Host phase instructions and complete operator notices contribute
to normal-decision reserves. Summary input is separately bounded at 48,000 tokens.
These are working-policy limits, not advertised model-window sizes. Invalid window,
trigger or target combinations fail configuration rather than silently increasing
limits. UTF-8 byte counts remain separately recorded for files and working memory.

Large tool outputs are content-addressed and replaced by retrievable references.
The overlay **rechecks size after archival**. If that representation already fits,
it saves an `archive_only` checkpoint without calling a summary model. When actual
summarization is needed, recent tool calls/results stay paired; old details remain
in the original archive. Working memory targets 2,400 UTF-8 bytes and has a validated
6,000-byte ceiling. Oversized responses receive diagnostics and bounded retries,
never silent truncation. Requested output-token limits are not assumed enforced by
all provider adapters.

If no verified windows are configured, `/context/config` explicitly reports legacy
byte compatibility. Older Hosts without the new endpoint can still use that mode;
a temporary error from a new Host does not silently disable token control. A legacy
Harness that sends summaries to the ordinary model endpoint must be updated to use
the explicit summary operation before a separate summary route can apply.

## Deliver observations before compacting them

The overlay prepares context **before** the next model decision. The final complete
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
The generated main publishes `run-outcome.json` at its workspace root; only the
initial builder uses the `run/` subdirectory. Saving a checkpoint does not update
Agent-authored experiment decisions or stale job ledgers.

## Fixed roles and narrow summary operation

`MODEL_NAME`, `MODEL_CLASS` and `A3_MODEL_REASONING_EFFORT` remain the primary model
configuration. Optional `A3_WORKER_MODEL_NAME` and `A3_SUMMARY_MODEL_NAME`, with their
respective `A3_WORKER_REASONING_EFFORT` and `A3_SUMMARY_REASONING_EFFORT`, configure
fixed helper and summary routes. Absent overrides, behavior remains compatible.
All these settings are Host-owned; containers cannot supply upstream URLs, keys or
arbitrary role-model mappings. The supported service-tier override remains
`A3_MODEL_SERVICE_TIER=default` (Standard).

| Session or operation | Route and authoritative contract |
| --- | --- |
| Current main, root builder | Primary model |
| Formal candidate-main trial | Primary model and current phase contract |
| Ordinary helper/test assistant | Worker override, otherwise primary |
| Main-assigned `purpose: candidate_validation` session | Primary model and phase contract, without adoption authority |
| `POST /v1/summary` | Summary override, otherwise primary; data transformation only |

A test assistant and a model under test are different roles. Testing a future main's
behavior with a helper model would not establish behavior of the deployed main.
Only the current main may assign the ordinary-session `candidate_validation`
purpose. It changes neither artifact/data permissions nor the existing formal
trial/decision mechanism. Role and requested/actual model options are audited.

`POST /v1/summary` accepts `previous`, `fragment`, an optional strict boolean
`compact_retry`, and a relative
`context-history/snapshot-<SHA256>.json` source reference. It cannot receive client
instructions, tools, model selection or credentials. The Host supplies concise
source/uncertainty/correction instructions rather than repeating the whole phase
contract. The output grants no authority and never executes tools. The full phase
contract continues to govern normal main and candidate decisions.

Summary metadata binds operator-feedback IDs and hashes. It does not copy every
full notice into every fragment and does not mark a reference as full delivery.
Full notices remain durably stored on the Host and are still delivered unchanged
to that owner's normal decisions, including after compression and resume. They
are per-run records; a new task needs its own approved initial intervention notice.

To enable token control, set verified `A3_MODEL_CONTEXT_TOKENS` and, for alternate
models, `A3_WORKER_CONTEXT_TOKENS` / `A3_SUMMARY_CONTEXT_TOKENS`. Optional
`A3_CONTEXT_TRIGGER_TOKENS`, `A3_CONTEXT_TARGET_TOKENS` and
`A3_SUMMARY_INPUT_TOKENS` tune the working policy. Model identifiers and windows are
deployment choices; no particular provider, account or model is a public default.

## Durable failure behavior and deployment

Summary attempts are persisted **before** dispatch with completed fragments.
A restart reuses completed work and does not reset the eight-call budget for the
same checkpoint, tools, implementation and configuration. Transient failures have
at most three attempts per fragment with bounded backoff, within that total.
A plan that cannot finish in the remaining budget blocks honestly before spending
more. Corrupt accounting is rejected rather than silently reset.
Oversized responses keep the original previous memory and transcript fragment.
A persisted corrective-retry flag selects a fixed, tighter Host instruction on
the next existing attempt; it cannot select arbitrary instructions, tools or
models. The correction does not raise the 6000 UTF-8 byte limit, truncate evidence
or reset the cumulative attempt budget. Token reserves cover the larger fixed
corrective prompt, even on normal summary attempts.

Resume and continuation manifests are published before checkpoints that may cite
new archives. File and parent-directory writes are synchronized. The tool journal
keeps an idle `null` file; interrupted operations remain unknown and are not blindly
submitted again. These measures are not a multi-file power-loss transaction or a
promise of perfect semantic summaries. Persistent provider/storage/budget failures
can still require an explicit operator repair.

Enumerate the copied helper and every required file in `run.json`, `resume.json`
and `continuation.json`. The ordinary entry remains `python agent.py --resume`.
Starting a new task uses existing `--run-harness` / `--agent-inputs-dir`; do not edit
historical outcomes into new success labels. For a paused process, editing disk
modules does not reload already imported Python code. Any later authorized update
must preserve the checkpoint/journal and deliberately restart only the owned
process through the existing stopped-main recovery path; ordinary unpause alone
is insufficient. Record the intervention in resume feedback.

Offline tests cover archive-only behavior, role/auth boundaries, injection reserves,
token/byte separation, persistent retry accounting and storage/journal failure.
Real validation must separately check actual response models/options, semantic
corrections/unknowns, source reads, normal tool work after recovery, and reference
reachability. Neither a process exit code nor a mock response proves those facts.


## Durable operational memory and bounded analysis

The optional overlay now returns normal text reads as bounded character-page objects:
`path`, whole-file `sha256`, `offset`, `end`, `total`, `eof`, and `content`.
Offsets refer to decoded characters, not UTF-8 bytes. Pages are bounded by actual
serialized size. Optional top-level JSON field selection retains its existing
contract. `operational-state.json` records retrieved ranges separately from exact
page payloads included in a successful completed model request. Neither proves
understanding. Sparse coverage is bounded with an explicit incompleteness marker;
gaps are never merged into unread coverage. Views and receipts have finite caps.

The `progress` tool records explicit Agent-authored open/waiting/closed items and
workspace-relative evidence references; it does not write acceptance or decisions.
Missing/large/changed evidence is uncertain, not automatically passed or reopened.
Current operational facts are attached as a bounded ephemeral frame on each normal
decision, outside rolling summaries and the saved model history. Its estimated
cost is reserved before compression. The exact frame is archived, references and
new modules enter ordinary resume/continuation manifests, and a corrupt sidecar
fails closed rather than silently resetting it. One previous sidecar and separate
progress-item archives bound redundant storage while preserving task changes.

Same-task resume and handoff preserve this ledger and analyzer cadence. A genuinely
new task archives and resets task-specific operational state and analysis schedule
in the new copy only; skills, lessons, source packages and old archives remain.
The analyzer contract, automatic schedule and limits are described in
[trajectory analysis](trajectory-analysis.md). This is an implemented opt-in overlay,
not evidence of production loading or long-run research improvement.
