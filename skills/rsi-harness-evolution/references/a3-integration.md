# Actual A3 integration and evidence boundaries

Checked 2026-11-02 against repository 6bac90b and a read-only export of F ready retained run `run-20261102-182101-579869`, version `version-97dba45d8fb245c3914b0663784fcf9c`, snapshot `30cf94949073f172dd005aa59a04e3c8768d83ee`. G input preparation at repository commit `28aeef6` is also included in the validated source tree; preparation is not an executed G result. These are inspected versions, not promises about a future deployed run.

## What the inherited F code actually provides

- `agent.py` builds model requests from the full `messages` list, appends outputs and tool results, and saves/restores the full checkpoint. This version has no token-budget compaction. G is intended to add context management; inspect its resulting implementation rather than assuming it has already done so.
- Ordinary tools include workspace `read`, `write`, `list`, a bounded `wait`, gateway session/job/version/evolution calls, and E/F web/skill tools. There is no generic local shell tool in this F implementation. Remote jobs use the existing command-array/file interface and their own environment.
- `skill_discover` searches metadata. `skill_read` returns the catalog entry, not the Markdown body. Read `local_source` with the ordinary file-read tool and load only the relevant references.
- `skill_execute` is hardcoded for `synthetic-report-checker`; registering this reference skill does not make its files executable or add a new tool route.
- F's normal manifest writer recursively includes non-symlink files, excluding `__pycache__` and `.tmp`; referenced assets still need to be present. The checker-specific source/evidence hash rules do not automatically validate every new skill.

The corresponding `skill_tools.py` SHA256 is `aba683ccb97fb0c92d949dff9e4f3a7187e1c956f3614f5f4150e4238871c9fc`. Use actual evidence from the current run when making stronger statements.

## Add this skill to a new task without another launcher

The repository directory is a versioned source artifact, not automatically loaded by all Agents. For a future task using the existing `--run-harness` and complete custom input directory, include the skill tree among its files. The initial task notice can identify its `/run/input` location and instruct the Agent to read or copy it into its own workspace through existing tools, then merge [catalog-entry.json](../assets/catalog-entry.json) by ID into the existing `skills/index.json` skills array. Preserve unrelated entries and the original source package.

Verify metadata discovery, body/reference readability and persistence after the next ordinary checkpoint. Do not claim the skill was adopted merely because its files were published. Historical F packages are not rewritten. A live run's actual loading and use require its own evidence.

## Apply the right contract

Current execution.md describes `/jobs`, `/versions`, `/evolution` and session lifecycle contracts. Ordinary `POST /sessions` creation is denied to Agent tokens. Inspect those definitions before using them; this skill does not rename them to invented APIs such as `selfmodifyharness`. The current Agent implements and tests components itself. A complete main-harness candidate uses the existing trial/decision and main-owner checks when adopted. Local edits do not automatically reload a running process.

The new RSI skill complements `a3-mujoco-autoresearch`: the latter helps choose and test robot-learning changes; this one helps improve the Agent's research machinery. Keep source versions and outcomes separately attributable. A current capability task such as G uses only its own acceptance, not every optimization direction here or the entire phase-two protocol.

## Validation scope

This package is externally authored research guidance and an optional JSON experiment template. No runtime optimizer, memory service, generic skill executor or new model route is installed. No actual RSI performance improvement is claimed. Validation status and its limitations are in [validation.md](validation.md).
