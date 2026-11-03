# Research coordination and bounded workers

The main Agent chooses direction, assigns Policy and Harness work, coordinates dependencies,
inspects evidence and makes decisions. Concrete research implementation, collection, training
and evaluation belong to workers. This division does not require another worker for a simple
coordination action. The [orchestrator skill](../skills/research-orchestrator/SKILL.md) is an
optional toolbox: choose, adjust, skip or combine methods; it is not a fixed workflow.

## Existing authority and optional Harness support

The Host still has ordinary `purpose=worker` sessions, separate workspaces, the worker model
route and unchanged resource limits. Policy/Harness are assignment roles, not new Host
capabilities. Raw API availability does not authorize the main Agent to perform worker work.
The optional recovery Harness supplies `delegate` operations; existing gateway calls remain
usable inside the same responsibilities and permissions:

- `assign`: id, role (`policy` or `harness`), objective, deliverables, stop_condition,
  selected context_files and read_only_exports. It constructs a complete bounded source
  package, fresh checkpoint and explicit worker-task.json; it never clones the main dialogue.
- `collect`: retrieve a stopped worker's report and exact referenced artifacts through the
  authorized session interface, verify hashes and accessible ready exports, then save evidence. An explicitly stopped invalid delivery may be rejected
  with reasons without pretending its scientific outcome is known.
- `decide`: record adopt, reject or follow_up with a reason after evidence collection. This
  does not mark scientific acceptance, choose a Host trial winner or execute integration.
- `list`: bounded operational assignment state, retained outside rolling model summaries.
- `reconcile`: inspect accessible sessions for a matching assignment and source hashes after
  an uncertain creation response. It never automatically resubmits an uncertain request.

These are independent operations, not a required sequence for every coordination action.
A worker runs `python agent.py --resume --worker-task worker-task.json`. Its own completion
contract is worker-result.json, not the entire run's acceptance. Resume/continuation preserves
that entry command; reports remain claims for the main Agent to evaluate. Reports accept only the declared fields,
with a text summary of at most 4000 characters; detailed logs belong in hashed evidence files. A worker cannot
promote itself or inherit another identity's credentials. Test helpers and candidate-validation
sessions retain their distinct model-purpose contract.

The standard Harness adapter rejects direct main POST/jobs, including equivalent query and
trailing-slash URLs; it keeps result inspection and coordination available. This is an
execution contract of that loaded Harness, not a new security boundary in Host. The main
must also follow its role when designing work and using other tools.

## Context, references and assets

Only explicitly selected research or collected-result files accompany an assignment. Core
source hashes and the bounded task are recorded before creating the session. Research
reference skills are materialized from active frozen input hashes and copied with their
catalog/provenance for on-demand discovery. Whole reference texts are not automatically
injected into every decision. Existing Agent-improved skill entries and old reference
versions are preserved; publication is not evidence of performance improvement.

Workers must explicitly receive the relevant frozen goal/protocol/conditions, authorized data
and stop conditions. A remote job ID alone is not a returned asset: the worker seals a ready
export and grants it to current main before returning it. Main can then pass an accessible
export via read_only_exports. No source_owner is forged and no token is shared.

The ledger reserves creation before POST. A missing response remains uncertain; inspect and
reconcile instead of replaying. Completed decisions are archived when the active ledger fills,
not used as a lifetime research limit. Each assignment has a fresh nonce and full
task hash, so an archived ID cannot bind a new question to an old worker. Failed or unavailable delivery is not scientific
failure or success and cannot be adopted as a verified result; rejection does not cancel
uninspected remote jobs.

## Integration, handoff and remaining boundary

When a Harness worker returns tested work, main promptly decides at a safe Policy boundary,
then performs required version-specific validation before integration or handoff. It must
not orphan active workers: a successor normally cannot manage the predecessor's other
children. The adapter checks the authorized descendant status snapshot before retaining a
candidate and refuses while other workers are active or unknown; it never cancels them to
force handoff. Settle work and explicitly transfer accessible evidence and sealed exports.
Self-status returns only is_main/has_been_main role flags, not other identities. A former
coordinator saves its checkpoint and exits instead of continuing research after handoff.

A worker that exits before sealing/granting remote assets can leave results its main cannot
read directly. This implementation honestly records incomplete delivery; it does not add an
implicit remote-owner bypass or generic worker restart permission. Full autonomous recovery
of that case would need a separately reviewed, bounded Host lifecycle operation preserving
the same stopped worker identity, scope, checkpoint and job references. Existing external
operator recovery remains a distinct intervention and must be recorded as such.

Tests use real local worker processes and Host routing with a fake provider/remote backend,
plus a test-only workspace mount emulation. They establish task isolation, routing, report
verification and coordination contracts, not Docker security, real training quality or a
long-duration reliability claim. Source synchronization does not hotload a running Agent.
