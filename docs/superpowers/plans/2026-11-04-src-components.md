# Component Source Migration Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans for integration; component work may be delegated with exclusive file ownership. Verify each task before local commit.

**Goal:** Deliver an installable component-oriented source tree with compatible legacy entry points and complete execution payloads.

**Architecture:** Single src package, Host/Controller/Harness dependency boundaries, explicit payload assembly and legacy adapters. Keep persisted formats unchanged.

**Tech Stack:** Existing Python, setuptools, pytest, Ruff, FastAPI and standard-library Controller.

**Spec:** docs/superpowers/specs/2026-11-04-src-components-design.md

## Global Constraints

Follow every constraint in the spec. No production mutation, push, deployment, model/GPU calls or shared-resource cleanup. Local commits are authorized.

## Review Focus

- Installed package cannot accidentally depend on checkout PYTHONPATH or omitted modules.
- Gateway route extraction shares one state/lock and preserves authorization and checkpoints.
- Controller subprocesses use the full new implementation and original shared lock/storage paths.
- Harness payloads contain actual transitive sources and retain existing flat entry compatibility.
- Recovery preserves uncertain outcomes and complete historical state without replaying operations.

## Task 1: Isolated baseline and package foundation

- [x] Pin independent checkout to f2b83b9; read recent repair diff.
- [x] Run baseline offline suite with temporary localhost fake providers only; record failures.
- [x] Add failing package/install and legacy entry tests; configure src package discovery.
- [x] Establish canonical imports, explicit project paths and legacy module compatibility helper.
- [x] Verify focused tests and commit locally.

## Task 2: Host component migration

- [x] Move local modules into host sessions/runs/inputs/models/web/telemetry and workspace leaves.
- [x] Preserve old runtime.local imports as adapters; retain script CLI behavior.
- [x] Split Gateway schemas/state/route registration and launch CLI/transport/workspace/task preparation.
- [x] Run Host, ownership, evolution, resume, input and lifecycle regression tests; commit locally.

## Task 3: Controller migration

- [x] Move execution source into canonical package; split job store, scheduling and lifecycle/CLI responsibilities.
- [x] Preserve standalone legacy script and its watcher/sealer modes, standard-library dependencies and locks.
- [x] Add subprocess/legacy equivalence tests; run remote regression tests; commit locally.

## Task 4: Harness migration and payload completeness

- [x] Move overlay into context/memory/orchestration components with import-safe agent entry.
- [x] Preserve flat commands and source-delivery contracts via explicit payload assembly.
- [x] Cover latest workspace assembly, child collection and retry-safety fixes without weakening checks.
- [x] Run full Harness/orchestration fixtures including actual child processes; commit locally.

## Task 5: Integration and documentation

- [x] Verify sdist/wheel in a clean noneditable install and arbitrary cwd; inspect distribution contents.
- [x] Run complete offline suite, Ruff, format and diff checks; retain failure details.
- [x] Synchronize README, project structure, setup, architecture and payload/Controller documentation.
- [x] Obtain whole-branch independent review; fix important findings with regression tests.
- [x] Leave committed branch available, no production merge/push/deployment.

## Verification and delivery

- Full isolated regression: 1,125 passed.
- Ruff, repository-wide formatter and diff checks passed; changed-document local links resolved.
- Controller stage: 98 focused tests. Host stage: 159 focused tests. Harness stage: 324 focused tests, including five actual child-process/localhost fake-provider scenarios.
- Artifact tests build wheel/sdist, install the wheel into a temporary target, exercise the Controller under `-I -S`, validate Host CLI outside a checkout, export complete Harness sources, and rebuild identical package bytes from the sdist.
- Independent whole-branch review found no substantive issues; reviewer independently ran 12 scoped tests.
- Existing runtime dependency versions are unchanged. The declared setuptools build backend is also a locked dev dependency so artifact tests run in the development environment.
- Branch is preserved for later integration. No production merge, push, deployment, live Agent changes, real provider calls, SSH, GPU or real Docker validation was performed. Offline regression is not experimental acceptance.

## Local follow-up acceptance: 2026-11-05

- Integrated `e1bd729` into the migrated layout, preserving legacy adapters.
- Final requested policy: 500k configured context, approximately400k estimated total-input trigger,32k post-compaction soft target. Separate hard admission remains441,808 tokens.
- Complete configuration is in `configs/context-400k.env.example`. It is not automatically applied; existing deployment values and active processes remain unchanged.
- Large reductions use up to16 persisted fragment attempts only when eight conservative fragments cannot cover the trigger. Small policies/byte mode keep eight; each summary input stays bounded at48k. Downgrading a policy preserves spent accounting instead of writing an invalid record.
- SOUL/G/working coordination guidance is shorter and adaptive; A3 knowledge material and scientific protocols are unchanged. Local reliability adoption is judged against its original purpose, without an added efficiency-gain requirement. Existing trial/ownership/evidence-access controls remain unchanged.
- Full combined offline acceptance:1,142 passed. Ruff,235-file format check, local links and skill validation passed. Independent review findings were resolved.
- A deterministic offline fixture reduced410,281 estimated input tokens to25,408, using11 fragment requests; replay made no additional requests. This validates budget/persistence mechanics, not real-model summary quality.
- Branch is accepted for local integration. No production merge, push, deployment or experimental acceptance decision was performed.


### User correction: one full-transcript summary (2026-11-05)

Supersedes the preceding segmented-summary policy. Trigger remains 400k, soft target
32k and configured context window 500k. Submit the complete readable transcript once
to `/v1/summary`; reserve output/framing within that window. Remove the independent
48k input setting, chunks, automatic summary retries, 8/16-call budgets, byte mode
and old Host/protocol fallbacks. Keep lossless archives, paired fresh tool results,
and durable dispatch/completion records so recovery never blindly repeats a request.
`A3_SUMMARY_INPUT_TOKENS` must be removed at a later authorized deployment. No live
process or production configuration is changed by this local branch update.


Validation for the single-pass correction: complete suite **1144 passed in 70.94s**;
subsequent focused full-window/unknown-dispatch checks **5 passed in 3.49s**.
Ruff, formatting and diff checks pass. Independent read-only review found no
substantive issue and independently reran the five single-pass checks. Tests use
offline callbacks and validate transport/state behavior, not semantic summary quality.
Production remains unchanged at `f2b83b9`; no push, deployment or experiment changes.
