# Assemble a fixed snapshot with selected workspace files

This optional `/sessions` input form lets an Agent execute a worker-written program using
an accessible immutable source version plus a few newly written research files. It removes
the need to copy an entire source tree through model text. It is a transport method, not a
research sequence, new identity or permission to execute code in the main process.

`parent_version` still records ancestry only. Ordinary `files` uploads remain complete
replacements; they do not inherit omitted files. `version_id` without attachments still
executes exactly that saved version. None of these forms scans a caller's current workspace.

## Agent-visible calls

An Agent can write its runner and preregistration with its existing `write` tool, then obtain
their whole-file SHA256 from bounded `read` results. `GET /versions` lists snapshots accessible
to that identity; select a version using its recorded source identity and inputs, not merely
its position in a list. Verify that the chosen base includes the intended dependencies.

Submit only the selected identifiers and references through the existing `gateway` tool:

```json
{
  "method": "POST",
  "path": "/sessions",
  "body": {
    "command": ["python", "research/prepare.py"],
    "version_id": "<accessible fixed source version>",
    "workspace_files": [
      {"path": "research/prepare.py", "sha256": "<64 lowercase hex characters>"},
      {"path": "research/prereg.json", "sha256": "<64 lowercase hex characters>"}
    ]
  }
}
```

The placeholders must be replaced with real values. The Host reads those files from the
**authenticated caller's own** workspace, checks the bytes and creates a new immutable
version containing the full base plus these explicit same-path attachments. It does not
ask the model to construct or encode a full `files` dictionary. The child runs the requested
command in the ordinary isolated session with its own credential and existing limits.
`workspace_files` cannot be combined with ordinary `files` or `parent_version`, and requires
`version_id`. It cannot name another source owner or session.

If a selected path already exists in the base, the attachment explicitly replaces that path
in the derived version; the base remains unchanged. File/directory prefix conflicts fail
before saving a derived version or creating a container. This form does not freshen an
inherited Agent conversation: select an explicit preparation program rather than blindly
resuming a different task's Agent entrypoint. Current workspace checkpoints, environment,
credentials and unselected new files are not automatically added.

## Bounds and provenance

- Up to 16 unique, canonical relative paths under `research/`; source and destination paths
  are identical. Hidden components, absolute paths, aliases and parent traversal fail closed.
- Each attachment is a regular UTF8 file, at most 1 MiB; total attachments at most 8 MiB.
  Descriptor-relative no-follow reads reject parent and leaf symlinks and nonregular files.
  A changed file with the wrong expected digest is rejected, not silently refreshed.
- The base requires existing version access. Its bytes and checksum are read under bounded
  snapshot loading. The assembled result still obeys 128 files and 20 MiB. These are input
  limits, not new CPU/GPU or filesystem permissions.
- Validation failure does not launch a child or save a derived version. Later resource or
  launch failure can still leave a saved immutable version, as with ordinary `files` upload;
  inspect existing state rather than blindly retrying an uncertain creation response.

The response's `assembly` contains base and derived version IDs/source hashes, plus each
attachment's relative path, actual SHA256 and byte count. The receipt is also recorded with
the session and in session events. It identifies the assembled bytes, not program correctness,
pre-registration quality, successful execution or scientific acceptance. Hashes are not proof
that a worker executed its claims.

Read status and artifacts through existing authorized session interfaces. A Worker cannot
read sibling or unrelated sessions merely because it knows their paths. A program producing
prepared envelopes must still deliver them through the supported collection contract;
this feature does not change `assign_prepared` evidence, task or identity checks.

Formal `/evolution/trials` explicitly rejects `workspace_files`: first assemble, review and
freeze a version, then select that version through the existing trial interface. Ordinary
candidate-validation purpose checks, read-only asset grants, roles and model routing remain
unchanged. Root identities without an actual candidate workspace cannot use workspace-file
attachments; ordinary complete uploads and authorized saved versions still work.

## Verification scope

Tests run the actual recovery Harness `write`, `read`, `gateway` and artifact calls against
production assembly code. The submitted request contains only command, version ID and file
references; no fixture fills a complete source dictionary for that call. Provider decisions
are scripted, so this proves tool reachability and data flow, not autonomous real-model
planning. A separate real-Docker check covers the same route without paid model or remote
job calls. Source publication alone never deploys this to an active run.

## Send selected workspace files to a remote job

`POST /jobs` also accepts `workspace_files`, without a version or extra local session:

```json
{
  "command": ["python", "runner.py"],
  "gpu": false,
  "workspace_files": [
    {
      "path": "orchestration/results/task/research/runner.py",
      "sha256": "<64 lowercase hex characters>",
      "destination": "runner.py"
    }
  ],
  "input_exports": ["<already authorized export ID>"]
}
```

For jobs, sources may be under the authenticated caller's `research/` or
`orchestration/results/`. Collection must already have delivered the files into that
caller's workspace; a reference cannot read a sibling's workspace. `destination` is an
optional canonical relative job path, defaulting to the source path. Jobs use the same
bounded, no-follow, hash-checked reads as session attachments. Ordinary `files` may supply
additional content strings, but duplicate destinations, file/directory conflicts and
combined inputs exceeding 128 files or 20 MiB are rejected before remote submission.
The 16-reference, 1 MiB-per-file and 8 MiB-total reference limits still apply.

The Host sends materialized strings using the unchanged controller protocol. The response's
`source_files` and Host `job-inputs.jsonl` associate each path, destination, actual digest
and byte count with the returned job ID. Remote status and artifacts confirm execution;
the receipt alone confirms materialization. If submission has an uncertain outcome,
reconcile recorded requests and jobs before retrying.

A worker submits under its own identity and uses only its own existing `input_exports`
grants. It need not create a child with asset access merely to read and serialize its
source. This does not broaden export sharing, enable arbitrary Host paths, or add
`version_id` to `/jobs`. The session interface above retains its research-only source
paths and immutable-version requirements. Deploy the updated Host before using this form;
publishing source changes does not alter a running Host process.
