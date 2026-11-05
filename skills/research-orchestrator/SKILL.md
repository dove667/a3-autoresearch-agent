---
name: research-orchestrator
description: Coordinate Policy and Harness work, decide which tested changes to use, and hand off through existing A3 interfaces.
metadata:
  version: 0.1.4
---

# Research orchestration

Use this as optional guidance, not a checklist, fixed sequence or scheduler. Choose the work, methods and degree of parallelism for the current goal, dependencies and available resources. Read [A3 research](../a3-mujoco-autoresearch/SKILL.md), [Harness methods](../rsi-harness-evolution/SKILL.md) or their references only when useful.

Assign concrete implementation, test execution and analysis to workers; the main issues required main-identity dispatches, reviews results and decides adoption, rejection and succession. Simple coordination needs no extra worker. Give each assignment its purpose, sufficient runnable sources and inputs, deliverables and a stopping point; reuse compatible completed work. Existing `delegate` operations or gateway interfaces are alternatives, not a mandatory call sequence.

Judge a change against its original purpose. A reliability repair or needed capability may be retained without a measured efficiency gain. Decide promptly when the relevant uncertainty is resolved; otherwise name the missing information and arrange only the work needed to resolve it. The run's scientific comparison still follows its protocol. Do not claim efficiency gains from unknown accounting or turn extra diagnostics into adoption requirements. [Bounded validation](references/bounded-validation.md) gives brief operational reminders.

Workers use their own identities and authorized resources. A path or job ID does not grant access. Deliver readable results through supported artifact collection and grant selected sealed exports explicitly. A timeout is not proof of failure: reconcile existing assignments and jobs before retrying. A fresh worker receives its own task, not the main's pending conversation. `parent_version` alone does not supply missing source files. Workers cannot impersonate the main or bypass guards.

A selected full-main replacement uses `/evolution/trials` and the existing current-main decision endpoint. Preserve the tested version, readable results, required state and assets; confirm the actual successor identity. Settle or explicitly transfer work that the successor could otherwise lose access to, without cancelling useful jobs just to hurry handoff. Local worker success alone does not transfer ownership.

For an authorized new workspace, the [catalog fragment](assets/catalog-entry.json) supports ordinary skill discovery and reading. Keep referenced files in the retained package. This reference does not install a scheduler or extend permissions; updating it does not hot-load an existing run.
