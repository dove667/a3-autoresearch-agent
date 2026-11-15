# Harness usability implementation plan

**Goal:** Reduce tool, retrieval and scheduling mistakes during autonomous research with smaller models, preserving research state and source evidence.

**Architecture:** Keep the single research Agent and server-owned execution queue. Expose common operations as validated tools, deliver the existing skill package through versioned inputs, and use a native-only local runtime and preserve scientific evidence through private offline migration. Scheduling owns resource estimates; research choices remain with the Agent.

**Constraints:** No formal research launch during maintenance. Preserve original trajectories, checkpoints, notes and exports. Public documentation uses generic deployment examples. Every changed current Agent-readable input receives a semantic version increment; historical prompts move unchanged to legacy_prompt/agent-inputs. Use existing dependencies, package manifests and test infrastructure. Implementation tasks have disjoint file ownership; integration and release follow after all tasks pass review.

## 1. Skills delivery and discovery

- Deliver the existing research skill tree with current shared inputs and published night releases. Require explicit versioned goal/acceptance/protocol inputs for new tasks; remove phase/profile selection.
- Rank multi-keyword skill queries by meaningful token matches rather than whole-query substring matching.
- Verify an actual prepared input snapshot installs a discoverable skill, local skill edits survive resume, and unrelated queries remain empty.
- Own input-source selection, input skill installation, skill registry and their tests; update the related input documentation.

## 2. GPU admission and queue efficiency

- Replace greedy whole-device reservations with bounded per-job reservations that honor explicit peak estimates and admit multiple small independent jobs.
- Reject impossible reservations explicitly, and retain temporary pressure in the same durable queued job.
- Inspect admission cost with a historical job population; eliminate avoidable duplicated probes without weakening shared locking or ownership checks.
- Preserve Docker/process histories, cancellation, retries and server-owned waiting. Verify shared-card admission and impossible/temporarily unavailable requests.
- Own execution scheduling, Controller queue/submission, transport configuration for scheduler options, related tests and process-backend documentation.

## 3. Structured everyday tools

- Add first-class job submission/status/artifact and bounded workspace search/edit operations. Compute attached file hashes locally; validate before submitting.
- Expose the already available web gateway through built-in tool definitions instead of optional historical extension modules. Avoid duplicate tool names when an extension is present.
- Preserve the generic gateway for explicit advanced calls. Keep all side effects behind the current journal and ownership boundaries; do not silently retry uncertain submissions.
- Give predictable invalid calls actionable structured errors, without exposing arbitrary exception text.
- Own the Agent dispatch/schema integration, new tool modules, packaging manifest and tests; update tool reference documentation and its input version.

## 4. Retrieval, waiting and historical state

- Use original trajectories to reproduce weak retrieval and stale-plan wait failures. Default Recall searches useful available history without requiring a guessed round window.
- Improve bounded durable-note retrieval and return source-linked results. Preserve superseded notes and original IDs.
- Ensure observed terminal jobs make waiting and plans converge; avoid repeated bookkeeping solely to get into a wait.
- Delete retired local Docker/builder execution, version trials, legacy schemas and adapters. Migrate the selected suspended run once outside the public runtime; keep research files in place.
- Published Harness updates reuse the same run, identity and research workspace. Keep checkpoint, plans, notes, skills and job state in place; archive release boundaries and input revisions separately.
- Summary size follows the post-compaction token target and actual model window. Remove arbitrary summary token/byte limits and delete the no-summary archive fallback. Required compaction must produce a complete summary before the next decision.
- Own memory, planning, waiting integration outside the Agent dispatch file, tests and context documentation.

## 5. Review, validate and deploy

- Run targeted regression tests after each task, then the complete suite, lint, formatting and package validation on the final snapshot.
- Perform a fresh independent review covering behavior, maintainability, documentation, interrupted submissions, current checkpoint recovery and published payload completeness.
- Verify a no-model local tool scenario, CPU-only remote job lifecycle and real shared-GPU admission when a short owned workload is necessary; release all owned resources.
- Push the reviewed version to the configured repository, back up and synchronize the execution source on the configured server, then verify commit/source hashes and health.
- Prepare the published Harness and versioned input release for the next operator-started experiment without starting research or replacing old evidence.
