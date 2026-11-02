# Discovery, execution and retention

This directory is a versioned skill source, not automatically injected into every run. Do not edit historical retained packages or hot-load an active experiment merely to make it available.

## Actual F interface inspected

Read-only export of F ready retained run `run-20261102-182101-579869`, version `version-97dba45d8fb245c3914b0663784fcf9c`, snapshot commit `30cf94949073f172dd005aa59a04e3c8768d83ee`, inspected 2026-11-02:

- `skill_discover({keyword})` searches catalog ID/description/keywords, returning ID/status/version.
- `skill_read({skill_id})` returns catalog metadata, **not** SKILL.md contents. Follow `local_source` with the ordinary workspace read tool, then only relevant references.
- `skill_execute({skill_id,report})` only allows the hardcoded `synthetic-report-checker`. It does **not** execute arbitrary helpers registered in JSON. This new reference entry must remain non-executable there.
- F's source/evidence hash freshness enforcement is special-cased for the synthetic checker. It does not automatically validate this package's whole tree.

The standalone entry in [catalog-entry.json](../assets/catalog-entry.json) is for a future workspace's existing `skills/index.json` skills array. Merge by ID without overwriting unrelated entries, then discover/read the new entry. Adding a catalog record alone is not proof of loading its body or executing its helper.

## Future authorized use

Provide this directory and merge the entry into a **new** workspace with an explicit intervention/task notice. Record package revision or a full file-hash manifest. Actual F save_manifest recursively collects regular workspace files, excluding symlinks, __pycache__ and .tmp files; verify the resulting declarations include every skill reference/helper rather than assuming mere input mounting retains them. Keep original E/F retained packages unchanged.

The current generic `--agent-inputs-dir` recursively snapshots regular UTF-8 files and rejects symlinks. A future complete task input directory can therefore include `skills/a3-mujoco-autoresearch/...` alongside its required task inputs. An explicit initial task/feedback notice can tell the Agent to read those frozen files under `/run/input/skills/...`, copy them with its ordinary read/write tools into `/workspace/skills/...`, and merge the catalog entry. No new launcher mode or live hot-load is needed. The input snapshot stays immutable; workspace skills can subsequently evolve with evidence. Publishing the repository alone does not perform these steps.

For helper execution, use the actual authorized execution surface. In F, that can be a declared CPU job with command/file payload through its job API, if the current task permits it. Do not invent a local shell tool or generic skill_execute route. The ordinary Python command in evaluation-helper.md describes the script's CLI, not an automatically installed Agent tool. No model/network/GPU is required for it.

Evidence states remain separate: catalog discoverability; body/reference readability; deterministic helper correctness; actual Agent use; robot rollout; comparative improvement. The present artifact can be a strong prior without being a proven A3 performance improvement.

## Package checks

Offline tests in `tests/skills/test_a3_evaluation.py` cover metric normalization, full-grid versus partial reporting, paired seed structure, strict types, missing/duplicate/corrupt records, conditions and protocol mismatch. These fixtures do not authenticate real reports. Parent delivery records the actual final test result and package commit; don't infer that a changed copy inherits the old result.

A temporary-workspace compatibility check against the exported actual F module passed on 2026-11-02: keyword discovery, metadata read, local-source body readability, refusal of unsupported execution, and preservation of the synthetic checker status. The source catalog SHA256 was 557da2941c58064d30e4422ff21a5e1a71a2669376fee33039dfe1416be90910 and remained unchanged. This was not a live research Agent invocation.

See [validation record](validation.md) for exact source hashes, test scope and limitations.
