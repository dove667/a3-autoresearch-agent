---
name: a3-job-recovery
description: Use when submitting A3 research scripts, reconciling an unknown submission,
  diagnosing queued jobs or inspecting execution health without opening held-out scores.
metadata:
  version: 0.1.2
---

# Submit and reconcile research jobs

The Controller's job ID identifies the execution. Save it with the submitted source receipt and intended outputs before waiting.

## Submit the actual source

Use the current `job_submit` tool and an argument-array command. Attach a local UTF-8 script under `scripts/` with `workspace_files`; the tool computes its SHA256. `files` contains literal UTF-8 source text. Existing datasets and weights use authorized `input_exports`.

```json
{
  "command": ["python", "scripts/act/shared/diagnostics/inspect_data.py"],
  "gpu": false,
  "workspace_files": [{"path": "scripts/act/shared/diagnostics/inspect_data.py"}]
}
```

Check the returned source receipt. Request a GPU for CUDA work; inspect the current tool schema when declaring memory or evaluation requirements. Submit independent ready jobs before waiting, and keep dependent jobs behind their prerequisites.

## Reconcile before retrying

| Observation | Next action |
| --- | --- |
| `queued` or resource contention | Keep the same ID and wait for admission. |
| `submission_unknown` with a job ID | Query that ID with `job_status`; use `job_list` to reconcile the authorized inventory. |
| Transport error without an ID | Preserve command, source digest, time and error; inspect the authorized inventory and matching outputs. Ask the operator to resolve identity if ambiguity remains. |
| Terminal execution error | Inspect logs, source and episode ledger; repair the demonstrated fault and identify work still needing execution. |
| Scientific `success:false` with a valid report | Retain the completed scientific observation. |

An absent or stalled output leaves the submission outcome unresolved. Preserve unknown attempts and reconcile them before submitting a replacement.

## Inspect health with bounded evidence

Read `job_status` and the expected manifest and ledger identity. Verify the exact declared episode IDs and count, including missing, duplicate or unexpected entries. For a held-out batch, count execution states and inspect failed or unknown entries while leaving episode scores unopened. Preserve ledger and report digests. Read the current episode-executor recovery contract before resuming: confirmed pending entries can use the same manifest and output directory; reconcile launching, running or failed entries first. Repeated identical wait responses call for checking those exact owned jobs and the current inventory. Record a concrete waiting defect for maintainers.

Read artifact pages with `job_artifact`; retain the source hash and distinguish a growing file from an immutable completed report. The current deployment input and tool schema define paths and supported operations.
