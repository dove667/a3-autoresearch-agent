# Context recovery overlays

`runtime/harness_recovery/` contains an opt-in repair overlay for a retained Harness
that already provides the compatible `web_tools.py` and `skill_tools.py` modules.
It is not imported by the Host and does not change historical retained versions.
Copying it is an operator intervention, not evidence that an earlier experiment
validated the new implementation. Record the source version, file hashes and
intervention in the new package provenance and initial human feedback.

## Recovery behavior

- Summary attempts are charged before dispatch and persisted with completed
  fragments. A restart reuses completed work rather than resetting the call limit.
- Working memory has a validated UTF-8 size ceiling (at most 6,000 bytes).
  Summary requests target a shorter compact state and preserve document paths and
  read coverage instead of copying reference documentation. Fragment planning
  reserves this memory headroom before dispatch. Oversized responses receive a
  diagnostic and bounded retries, rather than silent truncation. Provider adapters
  may discard requested token limits; the recorded 800-token output setting is a
  request, not an assumed provider guarantee.
- Transient failures receive at most three attempts per fragment with bounded
  backoff, within eight calls for the same checkpoint, tools, implementation and
  budget. An infeasible plan blocks before a request. Persistent failures remain
  blocked; repeatedly invoking resume does not create a fresh budget.
- Original transcripts remain content-addressed. Invalid progress accounting is
  reported rather than silently reset. Summaries retain source references and
  tool call/output boundaries are checked.
- Resume and continuation manifests are published before a checkpoint that may
  reference new archives. File and parent-directory writes are synchronized.
  The tool journal retains an idle `null` marker instead of removing a file named
  in the manifest. An interrupted tool call is recorded as an unknown outcome;
  it is not blindly submitted again.

These measures provide bounded recovery and preserve diagnostic state. A persistent
provider, storage or budget failure can still require operator repair. They are not
a claim of zero failures, a multi-file filesystem transaction, or perfect semantic
summarization. When resuming after intervention, explain the actual intervention
through the existing human-feedback mechanism.

## Package and verification

Create a separate source package from the selected retained snapshot, overlay only
`agent.py` and `context_manager.py`, preserve compatible tools and persistent memory,
and enumerate every required file in the normal `run.json` manifest. Do not edit
old outcomes or declare a new ready/pass result for this package. Start a new task
through the existing `--run-harness` and complete `--agent-inputs-dir` interfaces.
The ordinary entry remains `python agent.py --resume`; no new startup mode is needed.
The source checkpoint and old task are archived by the existing fresh-task path.

`tests/runtime/test_harness_context_recovery.py` covers transient and persistent
provider errors, malformed responses, durable attempt accounting, corrupt progress,
and storage/journal publication boundaries with a replaced provider. These are
controlled fault tests, not policy learning or real-provider availability evidence.
Real-model validation must additionally verify a normal tool step after recovery,
semantic corrections and unknown results, actual source reads, and default-command
restart behavior. A process exit code alone is insufficient.

## Host-owned inference settings

Deployments may optionally set `A3_MODEL_REASONING_EFFORT` and
`A3_MODEL_SERVICE_TIER=default`. They apply centrally to main, summary and worker
requests without exposing credentials or service configuration to the Harness.
Absent these settings, existing request behavior is preserved. Unsupported settings
fail validation. `default` selects Standard service; this option cannot select a
higher service tier. The model and authentication remain deployment configuration.

The Host records requested options and available response model/reasoning/tier
metadata. Verify actual responses after a model migration; changing a deployment
file alone does not establish that the provider used the requested configuration.
