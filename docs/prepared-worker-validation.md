# Fixed package-builder validation through existing worker identities

This optional method covers a candidate's pure package preparation followed by real Host
worker execution and delivery. It does **not** execute candidate Python in the production
main process or prove that the candidate's entire native `dispatch` ran. Use other scoped
validation for changes outside the package builder. A package digest identifies bytes;
it does not prove which code generated them or certify scientific acceptance.

## Preparation stays isolated

The main can use existing `POST /sessions` with a complete `files` snapshot or an accessible
`version_id` and a worker-written command. The ordinary child has its own identity and
workspace. Candidate code runs only there, with the frozen dependencies and selected inputs;
never pass the main token, its environment or a callback bound to its authority. The main
owns dispatch and acceptance; workers implement, execute and analyze. No new Host endpoint,
identity, model route or permission is introduced.

Within that isolated preparation session, the worker-written program can load the pinned
candidate and call its pure `worker_package(arguments, coordinator_session_id=recipient)`.
The recipient ID is task metadata, not a credential. Preparation must record actual code,
input and environment hashes, command, failures and test results. Do not claim an unexecuted
or malformed fixture is frozen validated evidence. Deliver the source module, preparation
receipts and a UTF8 JSON object as declared local evidence:

```json
{
  "version": 1,
  "module_sha256": "<SHA256 of the declared module file>",
  "task": "<the worker_package task object, not a string>",
  "files": "<the worker_package string-to-string files object, not a string>"
}
```

The example shows the field meanings; the two object fields must contain real JSON objects.
Use existing collection to verify each artifact into `orchestration/results/<assignment>/`.
The main reviews the preparation evidence before deciding whether to dispatch it. A trusted
collection receipt means only delivered bytes match the report, not that a producer's claim
of executing a particular module is true.

If the chosen immutable version already has the modules but lacks the newly written runner
or preregistration, use [workspace snapshot assembly](workspace-snapshot-assembly.md). The
Agent submits version ID plus its own research-file paths and full SHA256 values, not a
model-transcribed complete source dictionary. Host assembly, an admitted child process and
successful preparation remain separate facts; assembly alone does not prove execution.

## Data-only dispatch

`delegate` with `action: "assign_prepared"` requires the usual explicit assignment fields
(`id`, `role: "harness"`, `objective`, `stop_condition`, `deliverables`, `context_files`,
`read_only_exports`) plus:

- `package_path`, `package_sha256`: the collected envelope and its full-file digest.
- `module_path`, `module_sha256`: the collected candidate module and its digest.

Each selected source must have exactly one matching verified collection receipt. Current
receipts take priority; otherwise at most 16 matching archived assignments are read with
no-follow bounds and verified against their content-addressed archive filenames. Corrupt or
ambiguous archives fail closed. The main
must still have legitimate access to any explicitly selected ready exports; Host checks and
existing read-only grants remain unchanged. No asset or permission is inferred from a path.
Both collected files are regular, canonical, no-symlink reads bounded to 1 MiB **before** JSON
parsing. The decoded envelope is limited to 128 files, 1 MiB per file and the existing 20 MiB
upload limit; the smaller 1 MiB envelope transport bound often applies first. Larger packages
fail explicitly, without truncation or automatic retries.

The adapter checks exact task agreement with the main's assignment and current identity,
source hashes, selected context and file names. Ordinary context bytes must match the main's
selected local file; collected context also needs its verified collection receipt. A producer
cannot replace context by updating its own claimed hash. It rejects undeclared files, private/control
paths, normalization aliases, duplicate JSON keys and nonfinite constants. Core code files
are explicit payload, not arbitrary host paths. Optional `sha256:<digest>:<path>` context
selectors may carry hash-verified JSON only under `operator-recovered-artifacts/`; this copies
provided research bytes, never reads another identity's filesystem or changes a private
artifact grant. The prepared core code may be the candidate, but runs only in the new ordinary
worker, never in the coordinator.

Prepared control files must describe only the fresh bounded task. Trusted code then creates
a new nonce, `worker-task.json` and one-message `checkpoint.json`; it never adopts a producer's
conversation or executable entry command. It records package/module receipts in the ledger,
reserves before the existing `POST /sessions`, and keeps `purpose=worker` with the standard
worker entry command. An uncertain creation response remains `creation_uncertain`: use existing
`reconcile`, never replay the request under the same ID or rename it to bypass uncertainty.

## Interpretation and deployment

Collect actual child evidence and distinguish preparation, payload validation, Host delivery,
quality and efficiency. Charge both arms' preparation and coordination costs. A rejection by
this trusted validator is not proof the candidate itself enforced the same rejection.
The `generation_proven: false` receipt deliberately prevents interpreting source/hash metadata
as an execution attestation. Formal trial and succession still use existing main-only controls.
This adapter is not an acceptance label, production module hotloader or arbitrary-main runner.

Tests cover local processes and real Host routing against a scripted provider, not Docker
isolation, real-model quality or long-term scientific improvement. Publishing source does not
load it into an active Harness; deployment requires separate operator authorization and a
recorded safe update.
