---
name: a3-training-progress
description: Use when A3 training logs are too large to inspect directly, progress looks stalled or malformed and nonfinite records need investigation.
metadata:
  version: 0.1.0
---

# Inspect training progress

Combine the job's execution state with a bounded summary of its actual training records. Keep the original log available for diagnosis.

## Capture enough information

Use `job_status` for execution state and a bounded log tail. When a structured training log needs a scan, run an authorized read-only CPU inspection over the owned artifact. Stream records with bounded retained state instead of loading the whole log into memory.

Record the source identity, byte range or snapshot hash, bytes and lines examined, event counts, latest step and loss, latest checkpoint event, nonfinite values, explicit errors and malformed records. Bound the returned examples while preserving counts and links to the original ranges. Emit valid JSON even when the input contains NaN or infinity; represent these as explicit diagnostic fields.

## Handle a growing log

A running writer may end at a partial record. Keep that suffix and its byte offset for the next read; report it as pending. If the source becomes terminal and the record remains incomplete, retain it as a malformed record. A changed prefix, truncation or file replacement requires a new scan identity.

Artifact pages from a growing file may have different whole-file hashes. Combine pages only after verifying the same source prefix, or inspect a stable snapshot. Retain the observation time so an old completed scan cannot be mistaken for current progress.

## Decide the follow-up

An advancing step counter supports execution progress. Compare loss, checkpoint events, elapsed time and job state to localize a stall. A missing checkpoint log event calls for checking the recorded checkpoint output. Inspect stderr and the original records when the summary cannot explain an error. Keep scientific evaluation results separate from training-health diagnosis.

Scanning or summarizing logs should leave training and its saved state unchanged. Any decision to stop, repair or restart a job uses its verified identity and current evidence.
