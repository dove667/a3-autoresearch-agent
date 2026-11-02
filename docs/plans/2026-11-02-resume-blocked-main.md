# Explicit blocked-main resume implementation plan

**Goal:** Extend existing `--resume-from`, not add a new run entry point.
**Architecture:** Preflight the stopped current main and its external-blocker evidence, then journal an explicit restart before rotating only its scoped ephemeral credential. Reuse its workspace, identity and exact checkpoint; archive terminal declarations before setting active outcome to continue.
**Constraints:** No generated-code rewrites, context copying, feedback reinjection, live execution, commit or push. Ordinary interrupted resume stays unchanged. Completed runs, closed/retired workers and unconfirmed stops cannot be revived.

- [x] Add failing offline tests for nested Docker terminal records, invalid evidence/checkpoints, unchanged identity/history and token rotation.
- [x] Implement preflight and durable pending/complete restart journal in `run_resume.py`; integrate before gateway restoration and worker execution in `launch.py`.
- [x] Preserve terminal/console evidence and reject ambiguous pending restarts before issuing credentials or unpausing workers. Docker's fixed session name plus RunLease prevent duplicate starts.
- [x] Test gateway old-token rejection, same-scope new token, second self-stop, failure paths and unchanged ordinary resume.
- [x] Document explicit operator resume semantics and pending-restart review; run full offline pytest, ruff and diff checks.

**Review focus:** malformed/nested checkpoint tool calls; unsafe workspace/evidence paths; gateway/runtime self-stop disagreement; crash after container creation; current-main versus retired/closed identities.

Verification: full offline suite and repository Ruff/diff checks; no live resume or external calls. Pending failures are deliberately fail-closed and require inspection, not automatic reconciliation.
