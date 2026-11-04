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

- [ ] Pin independent checkout to f2b83b9; read recent repair diff.
- [ ] Run baseline offline suite with temporary localhost fake providers only; record failures.
- [ ] Add failing package/install and legacy entry tests; configure src package discovery.
- [ ] Establish canonical imports, explicit project paths and legacy module compatibility helper.
- [ ] Verify focused tests and commit locally.

## Task 2: Host component migration

- [ ] Move local modules into host sessions/runs/inputs/models/web/telemetry and workspace leaves.
- [ ] Preserve old runtime.local imports as adapters; retain script CLI behavior.
- [ ] Split Gateway schemas/state/route registration and launch CLI/transport/workspace/task preparation.
- [ ] Run Host, ownership, evolution, resume, input and lifecycle regression tests; commit locally.

## Task 3: Controller migration

- [ ] Move execution source into canonical package; split job store, scheduling and lifecycle/CLI responsibilities.
- [ ] Preserve standalone legacy script and its watcher/sealer modes, standard-library dependencies and locks.
- [ ] Add subprocess/legacy equivalence tests; run remote regression tests; commit locally.

## Task 4: Harness migration and payload completeness

- [ ] Move overlay into context/memory/orchestration components with import-safe agent entry.
- [ ] Preserve flat commands and source-delivery contracts via explicit payload assembly.
- [ ] Cover latest workspace assembly, child collection and retry-safety fixes without weakening checks.
- [ ] Run full Harness/orchestration fixtures including actual child processes; commit locally.

## Task 5: Integration and documentation

- [ ] Verify sdist/wheel in a clean noneditable install and arbitrary cwd; inspect distribution contents.
- [ ] Run complete offline suite, Ruff, format and diff checks; retain failure details.
- [ ] Synchronize README, project structure, setup, architecture and payload/Controller documentation.
- [ ] Obtain whole-branch independent review; fix important findings with regression tests.
- [ ] Leave committed branch available, no production merge/push/deployment.
