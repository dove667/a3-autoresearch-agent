# Explicit blocked-main resume implementation plan

> 历史方案；当前执行约定以 [单研究 Agent 工作流](../research-orchestration.md) 和 [执行接口](../../agent-inputs/shared/execution.md) 为准，旧 worker、分叉和派发描述不作为当前操作指引。

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

## Authorized notification follow-up

Use existing `--feedback-file` / HumanFeedback, not generated-code changes or hidden system instructions. Validate notification bytes before preflight; register the exact same bytes before restart and associate feedback ID/SHA with the attempt. Permit only the same current main's audited prior blocked restart to reopen its resulting continue/incomplete self-stop, with explicit feedback and verified archive hashes. Ordinary incomplete and completed/retired identities remain excluded. Offline tests capture the first real Gateway forwarding boundary: all 78 original history items remain, one user feedback is present, repeated histories do not duplicate it, and registered versus forwarded states remain distinct.
