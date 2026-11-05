# Component source layout

The compatibility design below is historical. The source-only completion in the
[implementation plan](../plans/2026-11-04-src-components.md#source-only-completion-2026-11-05)
supersedes it: maintained code/resources use the canonical package without legacy adapters.

## Intent

Move fixed infrastructure and the explicit derived Harness overlay into one installable
`src/a3_research_agent` package, with modules organized by responsibility. Keep current
CLI, HTTP, ownership, persistence, resource and delivery contracts compatible.

## Approach

A single source package with separate Host, execution and Harness components was selected.
Keeping the old runtime layout would retain checkout-only packaging; separate distributions
would add unnecessary release/version coordination. Legacy runtime paths become compatibility
entry points. New implementation imports use the canonical package. Controller remains
standard-library-only. Harness execution payloads explicitly include their transitive modules;
repository layout and payload layout need not be identical.

## Constraints

- Isolated local development only; no production configuration, run mutation, remote deployment,
  model calls, experiment interruption, push or merge into the production checkout.
- Base is f2b83b9, including workspace assembly, child artifact collection and retry diagnostics.
- Preserve source and evidence in historical runs; test recovery against independent fixtures.
- Keep existing dependency versions, permissions, upload limits and externally visible schemas.
- Preserve legacy launch, reload and Controller entry points. A3_PROJECT_ROOT already denotes
  the separate simulation repository; never reuse it as this infrastructure repository root.
- Python >=3.11 for the installed Host package; standalone Controller keeps its existing
  Python >=3.10 standard-library contract.
- Public documentation uses generic deployment paths; private environment details stay outside Git.

## Boundaries

Host owns gateway/auth, sessions, runs, versioned inputs, models, web and telemetry.
Execution owns job storage/lifecycle, GPU scheduling, readonly exports and image preparation.
Harness owns agent entry, context, memory and worker orchestration. The packaging component
assembles explicit payloads without executing candidate code or reading Host credentials.
Workspace path validation and token estimation are leaf dependencies.

## Compatibility

Keep runtime launch/reload/controller scripts as thin adapters. Keep legacy public Python
imports through module aliases; tests target canonical modules when patching implementation
internals. Explicit infrastructure project root is distinct from input roots and run roots.
Historical checkpoints and retained manifests are not rewritten. No acceptance claim transfers
from an old Harness to a newly derived payload.

## Delivery

Perform staged local commits, validate each component and then the complete branch. Keep
production untouched. Report baseline failures, environment limitations and unverified real
model/GPU behavior explicitly. Deploy only after separate authorization and safe cutover.
