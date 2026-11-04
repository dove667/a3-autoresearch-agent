---
name: research-orchestrator
description: Select and adapt coordination methods for Policy and Harness workers within an authorized research task, including assignments, evidence review, asset sharing and safe succession.
metadata:
  version: 0.1.3
---

# Research orchestration

The main Agent owns research direction and judgment, dispatches work including acceptance tests, coordinates dependencies, reviews evidence and decides adoption, rejection and succession. Policy workers implement data, policy, training, deployment and evaluation work. Harness workers implement, test and integrate Harness changes. The main does not take over concrete research implementation or training/evaluation when delegation is inconvenient. Main-identity dispatch, validation coordination, trial initiation and succession decisions are main responsibilities under existing interfaces, not worker test implementation. Delegating concrete tests does not delegate acceptance responsibility or prevent the main from issuing the coordination operations they require. Status reads, assignment updates, evidence review and decision records do not need another worker.

Use this skill as an optional method package, not a checklist, fixed sequence or requirement to apply every section. Select, adapt, skip or combine coordination methods according to the current goal, dependencies, resources and total cost. The division of responsibilities, scientific protocol, permission boundaries and safe succession constraints remain binding whichever method is chosen. Skipping a method does not authorize main-executed research jobs or abandonment of active workers.

This is reference guidance, not an installed scheduler, role-enforcement layer or new permission. Existing sessions can execute either kind of worker task; the Host's ordinary `worker` purpose does not distinguish Policy from Harness. Verify actual loaded code and behavior before claiming automatic dispatch, monitoring or result integration. Publishing or reading this skill does not make an existing run use it.

## Choose useful work, not a fixed sequence

Use the current goal, acceptance, protocol and authorized resources. Compare expected performance or information benefit against total elapsed time, compute, model calls, implementation and validation cost, risk and dependencies; preserve final evaluation and recovery budget. [Policy/Harness allocation](../rsi-harness-evolution/references/policy-harness-allocation.md) develops this choice. [A3 research](../a3-mujoco-autoresearch/SKILL.md) supplies optional Policy methods; [RSI](../rsi-harness-evolution/SKILL.md) supplies Harness methods. Read only what the chosen question needs.

Delegate concrete execution to a bounded worker assignment. Use parallel workers when valuable independent questions and real resource capacity justify them; use one worker, sequential assignments or waiting when they do not. Neither two permanent workers nor a training job is obligatory. A missing resource is a constraint to record, not a reason for the main to silently execute the work itself.

## Give a runnable, bounded assignment

When assigning execution, supply enough context to act and judge the result: the question, change boundary, applicable protocol, input/code versions, relevant prior evidence, dependencies, resources, deliverables and completion/stop conditions. Keep the record proportional to the task; preserve worker/version/job identities and uncertain outcomes for recovery. Shared edits need one owner. Reuse matching completed evidence rather than launching another worker to repeat it. For blocked delivery or validation, select only the relevant [bounded validation methods](references/bounded-validation.md); diagnostic checks do not automatically become new acceptance gates.

If an actually loaded Harness provides `delegate`, use its `assign`, `collect`, `decide` or `list` operations as needed; existing gateway routes remain an alternative. These operations are not a mandatory call sequence. Follow the contracts of the chosen route, including result schemas and retry reservations, without treating one helper's availability as proof that the current run uses it.

`POST /sessions` accepts a command array, files or an accessible `version_id`, and ordinary `purpose: worker`. Ordinary workers do not automatically receive the main's full phase instructions: pass the needed constraints explicitly. Check the deployed runner can execute a bounded worker task rather than pursue the main's entire acceptance. A fresh worker checkpoint must describe its own assignment, not replay the main's pending calls.

`parent_version` records ancestry; it does not merge missing files into an upload. Submit a complete runnable snapshot or use a complete accessible version. Do not make the main implement the worker's research program while preparing an assignment. An authorized worker runner or reusable entrypoint must perform the implementation; if it is absent, dispatch bounded runner preparation to a Harness worker. The inherited F interface has no generic local shell or arbitrary `skill_execute` capability.

## Keep identities, data and resources explicit

Workers use their own workspace, session and remote-job identity. They may submit authorized `/jobs`; they cannot infer access to another identity's files, jobs or exports from a path. Check current execution limits and available session/job slots before dispatch, reserve validation capacity, and queue work when necessary. Worker labels do not change model routing, GPU leases or Host limits. Only the current main may assign `candidate_validation`; it is not a substitute label for ordinary implementation work.

For large training assets, the producing worker publishes `/jobs/{job_id}/exports`, waits for a sealed `ready` snapshot, records its manifest/hash, then grants it through `/jobs/exports/{export_id}/grants` to the current main. The main can pass accessible snapshots to a new direct worker or trial via `read_only_exports`; that worker's remote job mounts selected `input_exports` read-only under `/inputs/{export_id}`. A local session does not acquire a remote filesystem mount merely from a grant.

Keep code, processors, statistics, datasets and model checkpoint references together with their scientific conditions. A source path, job ID or preparing export is not an inherited asset. Cross-run retention requires explicit `continuation.json.remote_exports` and the existing access checks; keep the source package unchanged. Seal assets before cleaning up producers or depending on their mutable outputs.

## Collect evidence and make decisions

For returned work, collect the finding and original evidence needed to judge the assigned question. Executed experiments need actual code/data/config versions, job/export references, comparison conditions, failures and unknowns. Distinguish functional tests, loaded execution and scientific improvement. Preserve raw evidence; do not replace it with a worker's pass label or analyzer advice.

The main can inspect descendant `/sessions/{id}/artifacts` with bounded pages and obtain submitted code through `/sessions/{id}/versions`. It cannot directly query another identity's remote job as its own. Have the worker copy small results into its report and explicitly grant sealed assets. Review against the original question and active protocol; record adopt, reject or bounded further testing and its reason. Assign concrete implementation, test execution and analysis to workers; the main issues required main-identity dispatches, reviews the resulting evidence and owns the acceptance decision. Do not stop an otherwise authorized validation merely because its coordinating dispatch requires the main identity; this does not authorize workers to impersonate it or bypass guards.

Timeouts, late reports and transport errors do not prove a job failed. Check existing identities, assignments and evidence before retrying. Preserve an uncertain side effect and its recovery reference; do not duplicate training to obtain a cleaner status. Independent analyzer findings remain advisory and receive an explicit reasoned response through the available analysis interface, not automatic acceptance.

## Close the Harness loop promptly

When a Harness worker finishes testing, prioritize its adoption decision at the next safe coordination boundary instead of continually adding Policy assignments. Name any missing evidence, bound the follow-up worker test and record when to decide again. Required version-specific validation and independent scientific review still apply. If selected, arrange integration against the tested version promptly; a worker's completion is neither forced adoption nor permission to change the benchmark.

For a complete main-Harness replacement, use `/evolution/trials` and the existing main-only decision endpoint. The candidate cannot promote itself; the current main judges the frozen checklist and supplies accessible evidence. Verify the successor's loaded version and current main ownership. Do not invent a second main owner or a new privilege-transfer protocol.

Before promotion, settle assignments beneath the retiring main: let useful work reach a safe stopping point, collect reports, secure ready assets and explicitly pass the selected references to the candidate. The successor is usually a sibling of those old workers and does not inherit their supervision or local-artifact access. Do not leave ongoing assignments dependent on that unavailable access. If they cannot yet be settled, record the concrete dependency and next decision point and defer promotion; the adoption judgment need not remain undecided. Do not cancel valuable jobs merely to hurry handoff. Close a producer session only after its work and required assets are secured; explicit cleanup can cancel its jobs.

## Discover and retain this reference

If selecting this package for an authorized new workspace, use the supplied [catalog fragment](assets/catalog-entry.json), which follows F's reference-only form. Include this directory and any referenced skill directories that will be used; merge the fragment by ID into the `skills/index.json` `skills` array without replacing unrelated entries. `skill_discover` searches metadata and `skill_read` returns metadata; read `local_source` with ordinary `read`. F's hardcoded `skill_execute` does not execute this skill. Already installed, compatible copies need no ritual reinstall.

Verify actual metadata discovery, body readability, package versions and retained manifest entries. This package documents source-inspected interfaces and a requested division of work; it does not certify an automatic coordinator implementation or live use. Do not hot-load an existing run without its separately authorized input-update procedure.
