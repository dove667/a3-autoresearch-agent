# Operational telemetry and advisory trajectory analysis

The Host exposes bounded counters through `GET /telemetry`. An authenticated Agent receives its own counters by default; `?scope=descendants` includes its descendants according to the Host's session ancestry. Siblings, ancestors and other roots are inaccessible. The response contains no owner IDs, paths, route strings, request IDs, query strings, request/response bodies, messages, error text/types, credentials, model names, or input source filenames. It is constructed from an explicit whitelist, never by returning or parsing raw diagnostic or usage logs.

The rolling window holds at most 256 authenticated completed requests across the Host, grouped into fixed request categories. `recent_events` contains the latest 32 authorized events in chronological order, each limited to kind, failure flag, actual observed duration and optional model role/numeric usage. Events contain no identifiers or timestamps. Snapshots deep-copy retained event data so callers cannot alter future observations. Unknown and control routes (`/telemetry`, `/analysis`, operator feedback/input controls, scheduler identity polls to `/sessions/self` and `/evolution`, health) do not contribute. Observed model durations use a monotonic clock around the provider call, including failures; request durations are measured separately. Only numeric bounded token usage fields are accepted. `usage_observed_calls` distinguishes missing usage from observed usage. Cost remains `null`: this interface does not infer prices or charges.

`POST /telemetry/observations` optionally replaces the caller's latest local counters. Supply exactly these seven integer fields, each between 0 and 1,000,000,000:

```json
{"read_calls":10,"repeated_reads":7,"retrieved_characters":1000,"delivered_reads":3,"open_items":2,"waiting_items":1,"closed_items":0}
```

There are no identity, path, free text or custom fields. Latest counters for at most 256 owners are retained and summed within the authorized scope. Host checkpoints persist them under `gateway.telemetry_observations`. Local observations are Agent-reported operational evidence, not independently verified task success. Gateway rolling request counters restart empty; the window is not a lifetime total. Consumers can post counters best effort once per decision; posting does not call a model.

The current main Agent explicitly invokes `POST /analysis` with no body (or `{}`), or an explicit `trigger` enum: `manual`, `repetition`, `no_progress`, `before_handoff`, `periodic` and retrieves the latest result through `GET /analysis`. Other owners are forbidden, including the old main after a transition. The POST reserves one call and returns HTTP 202 immediately with `status: running`; a single background worker makes the model request. There is no autonomous server trigger. The main Harness scheduler initiates key-event and periodic requests; the server only processes those explicit requests. GET retrieves running or cached terminal reports. The Host creates a fresh standalone request using the operator's worker model route/options, falling back to the main route when no worker override exists. The analyzer is a distinct advisory identity even when both roles use the same model. It receives only the authorized sanitized self-and-descendant snapshot, fixed untrusted-data instructions, no conversation history, no human feedback or phase instructions, no tools, no auth, and no sandbox/resource access.

The response has `advisory_only: true`, an opaque analysis ID, a status, and up to three enum-only suggestions. Actions include reducing repeated reads, reviewing failures/waiting items, checkpointing progress or continuing observation. Evidence and confidence are also fixed enums. Arbitrary model text, unexpected fields, tool calls, multiple answers and oversized output are rejected. The main Agent must evaluate suggestions itself and explicitly post `analysis_id`, `decision` (`adopt`, `reject`, `defer`) and a reason of 1–500 characters to `POST /analysis/response`. The exact report must belong to the current main. Dispositions are persisted and owner-returned; their reasons are excluded from telemetry and every analyzer input. This records consideration without forcing research to halt. `GET /analysis/{analysis_id}` retrieves an exact retained report for the current main. `GET /analysis` also exposes the oldest three unanswered completed reports under `pending_reports`, plus `omitted_pending_reports`; answering one reveals the next pending item. Pending summaries contain only IDs, status, trigger and fixed-vocabulary suggestions. The `history` object explicitly labels its Host-run scope, 32-report retention limit and cumulative `evicted_reports`/`evicted_pending_reports`, so bounded history is not presented as complete. New periodic reports remain permitted while older findings await a response. They are never operator feedback, pass/fail judgments, authorization, or automatically executed decisions.

Each call has at most 24,576 input bytes, 2,000 requested output tokens and 8,192 accepted output bytes. A Host-wide ledger permits one inflight call, a ten-minute cooldown and at most one model call per analysis. Exact repeated sanitized snapshots return their cached result for that owner. Control requests do not change the snapshot, preventing an analysis request itself from creating a new window. Reservations, failures and results persist in the existing private `RunStateStore` under top-level `trajectory_analysis`; callers must supply the Host's state store for durable restart behavior. Interrupted calls become `interrupted` and consume their reservation. Failed calls are cached and consume the cooldown; there is no automatic Host retry. The provider call receives `timeout=60` and `num_retries=0`, supported by the installed LiteLLM Responses adapter. Its current global retry default is unset. This installed adapter can let a changed global retry count override zero; operators must preserve the unset global retry setting. A custom transport that ignores its timeout can retain the sole inflight reservation; the Host never starts a replacement or nested worker to evade that limit. The ledger retains the latest 32 analyses, including their cached results and fingerprints; deduplication covers that bounded history. There is no lifetime analysis count cap. Provider error strings and raw outputs are never persisted there. Accepted numeric analyzer usage and actual elapsed provider/validation time are returned separately; unknown costs stay unknown.

Integration requires allowing `/telemetry` (including `/telemetry/observations`) and `/analysis` through the Agent gateway proxy. Main-client triggers can request repetition/no-progress analysis after at least ten minutes, a low-frequency thirty-minute fallback, and a before-handoff check; all requests share the same Host cooldown and deduplication. The Host does not recursively trigger analysis or nest workers. No Host restart, hotload, live model call or existing run mutation is part of implementation or testing. Tests use fake providers and `RunStateStore` fixtures. Production Host launch already supplies its state store.


## Automatic Harness schedule and signal limits

The supplied overlay starts a daemon scheduler automatically; it is not dependent
on a human or a main-model decision to request analysis. Every 60 seconds it checks
the current main identity and cached report. Analysis becomes periodically due at
30 minutes, including during legitimate job waiting. Event triggers supplement it:
10 repeats of already-retrieved, unchanged file hash/range within 10 minutes;
or at least 20 completed normal decisions and 10 minutes without a recorded
operational update or newly observed terminal job result. Trial creation requests
a before-handoff check. All triggers share a 10-minute cooldown, exact-snapshot
cache and one inflight reservation. Periodic work takes priority when due; cooldown
or an inflight analysis can defer it, not create concurrent analysis calls.

These are starting thresholds, not proven optimal values. The repetition threshold
aims to distinguish sustained duplicate reads from one legitimate retrieval;
validate it against the deployment's representative work, waits and false alarms.
At most six new analysis reservations per hour are normally possible, each with
the request bounds above; this is not a claim about actual money or enforced provider
billing. The 60-second polls themselves never invoke a model. Identity polls and
analysis/control traffic do not become new telemetry evidence.

Only fresh (under ten minutes) observed active-job status suppresses the no-progress
heuristic; stale status becomes unknown. Job-status polling alone is not a local
file-repeat signal. Same-byte decision rewrites and identical operational updates
do not count as new progress. Signals request independent review, never assert a
fault, cancel work or select a scientific result. Unknown semantic progress, omitted
read sources and jobs not observed by this Harness are limitations, not zeros.

The main decision receives a bounded advisory view and must explicitly respond
adopt/reject/defer with reasons, without a hard gate that blocks research. Faults
retain diagnostics and cooldown and leave research running; a damaged client
schedule is preserved instead of resetting rate limits. Same-task resume retains
cadence; a new task archives/resets task-scoped counters and reports in its new copy.
Simulation tests cover normal training waits, repetition, false-progress noise,
periodic coverage, failures, restarts and non-main identities. They do not establish
long-run analyzer usefulness or an absence of false positives and negatives.
