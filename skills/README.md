# Research skills

This is the project's single skill library. These methods support SOUL's long-term goal: improve A3 2×5 and 2×10 full-evaluation accuracy and success rate.

| Package | Read when |
| --- | --- |
| [a3-mujoco-autoresearch](a3-mujoco-autoresearch/SKILL.md) | Choosing methods, planning experiments, training and comparing policies |
| [a3-policy-integration](a3-policy-integration/SKILL.md) | Changing an adapter, task condition, checkpoint or generated runner |
| [a3-job-recovery](a3-job-recovery/SKILL.md) | Submitting scripts or reconciling uncertain jobs and batch health |
| [a3-frame-cache](a3-frame-cache/SKILL.md) | Diagnosing repeated decode cost or designing a shared frame cache |
| [a3-training-progress](a3-training-progress/SKILL.md) | Inspecting large, growing or malformed training logs |
| [a3-model-provenance](a3-model-provenance/SKILL.md) | Recording model identity, lineage, checkpoints and evaluation coverage |
| [a3-route-research](a3-route-research/SKILL.md) | Resolving research design choices about a new training recipe, architecture or adaptation method |

The Agent manages every skill through the same controlled `skills` tool. Start with semantic `list(query=...)` summaries, read a selected body with `read`, and use `list_resources` / `read_resource` for relevant attachments. Ordinary file tools do not access this directory. Skills are text and resources; execution uses remote jobs.

Use `save` to create or replace a body, `edit` for a hash-checked local body replacement, and `archive` to hide a skill from default discovery. Existing bodies require their current SHA256. Use `write_resource` / `edit_resource` / `delete_resource` for attachments, supplying the current attachment hash when replacing or deleting one. New attachments must not already exist.

Changes automatically advance the package patch version and retain the complete previous package under Host-managed `.runtime/skill-revisions/`. Each package has a `SKILL.md` entrypoint and `assets/catalog-entry.json`; skills remain independent of the three versioned prompts and continue across sessions and Harness upgrades.

Active long-term experience is stored separately in `memory/entries.jsonl` and managed by `memory`. See [prompt and skill management](../docs/reference/agent-inputs.md) and [storage layout](../docs/reference/storage-layout.md).
