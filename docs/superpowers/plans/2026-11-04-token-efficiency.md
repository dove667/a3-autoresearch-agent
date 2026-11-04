# Token efficiency implementation plan

Goal: reduce unchanged coordination round trips without losing instructions, results, or notifications.

Architecture: extend the existing wait tool with bounded status observation; keep existing feedback registration and replay semantics unchanged; publish a generated section index for on-demand interface reads. Core scientific inputs remain injected unchanged.

Scope: isolated development only. No live run mutation, provider probes, installation, or deployment.

- [x] Add failing behavior tests for long waits, worker state transitions, deadline and request errors; implement bounded polling inside one tool invocation.
- [x] Verify feedback: each registration stores only its own text. Preserve existing behavior per clarified requirement; no merge, deletion or semantic projection.
- [x] Add failing tests for a byte-identical execution reference and character-page index; clarify that current revision is already provided with each model request, and unchanged inputs need not be reread. Preserve all core inputs.
- [x] Run focused and full offline suites, Ruff and diff check. Obtain independent review. Measure deterministic fixture savings; keep quality and steady-state benefits unproven until authorized deployment.


## Follow-up: notifications while waiting

- [x] Add owner-scoped, read-only `/wait/signals` metadata and stable cursor without model calls or event consumption.
- [x] Poll notifications every 2 seconds and work states every 10 seconds; return on change, error or deadline. Bound each request and check notifications between slow work observations.
- [x] Recover cursor from paired wait results via the existing journal/checkpoint, freeze it across a single model response batch, and retain notifications racing expiry or crashes.
- [x] Run full offline regression and independent security/race review; document latency, request/log cost, at-least-once redelivery and no deployment.
