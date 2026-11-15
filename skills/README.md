# Research skills

These reviewed methods support SOUL's long-term goal: improve A3 2×5 and 2×10 full-evaluation accuracy and success rate. Each package contains a `SKILL.md` entrypoint and a versioned `assets/catalog-entry.json`.

| Package | Read when |
| --- | --- |
| [a3-mujoco-autoresearch](a3-mujoco-autoresearch/SKILL.md) | Choosing methods, planning experiments, training and comparing policies |
| [a3-policy-integration](a3-policy-integration/SKILL.md) | Changing an adapter, task condition, checkpoint or generated runner |
| [a3-job-recovery](a3-job-recovery/SKILL.md) | Submitting scripts or reconciling uncertain jobs and batch health |
| [a3-frame-cache](a3-frame-cache/SKILL.md) | Diagnosing repeated decode cost or designing a shared frame cache |
| [a3-training-progress](a3-training-progress/SKILL.md) | Inspecting large, growing or malformed training logs |
| [a3-model-provenance](a3-model-provenance/SKILL.md) | Recording model identity, lineage, checkpoints and evaluation coverage |

The Host snapshots every complete package here together with the three files in `prompt/`. An explicit input update or offline Harness release publishes changes. At initialization, the Agent installs frozen references into the shared `workspace/` and can search and read them with `skills`. An unchanged deployed package keeps its canonical ID on upgrade and archives the old entry. Agent changes to bodies, attachments or skill versions remain intact; the new deployed package then receives a separate reference ID.

Agent-created skills are saved in `workspace/skills/`, including the catalog and revisions. Active long-term experience is stored separately in `memory/entries.jsonl`. Future sessions and Harness upgrades reuse both directories. Promote useful workspace lessons here after reviewing current applicability, consolidating overlaps and removing local paths and experiment-specific instructions. Increment the package version whenever its content or attachments change.

See [input management](../docs/reference/agent-inputs.md) and [project structure](../docs/reference/storage-layout.md).
