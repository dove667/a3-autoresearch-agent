# Research skills

Each package contains a `SKILL.md` entrypoint and a versioned `assets/catalog-entry.json`. Packages in this directory are reviewed deployment inputs.

| Package | Read when |
| --- | --- |
| [a3-mujoco-autoresearch](a3-mujoco-autoresearch/SKILL.md) | Choosing methods, planning experiments, training and comparing policies |
| [a3-policy-integration](a3-policy-integration/SKILL.md) | Changing an adapter, task condition, checkpoint or generated runner |
| [a3-job-recovery](a3-job-recovery/SKILL.md) | Submitting scripts or reconciling uncertain jobs and batch health |
| [a3-frame-cache](a3-frame-cache/SKILL.md) | Diagnosing repeated decode cost or designing a shared frame cache |
| [a3-training-progress](a3-training-progress/SKILL.md) | Inspecting large, growing or malformed training logs |
| [a3-model-provenance](a3-model-provenance/SKILL.md) | Recording model identity, lineage, checkpoints and evaluation coverage |

New tasks snapshot every complete package here. An offline Harness release refreshes them in an existing task's input revision. At initialization, the Agent installs frozen references into its writable workspace and can search and read them with `skills`. Installation preserves Agent revisions and uses a separate reference ID when a new deployed version differs.

Agent-created skills are saved in `workspace/<run>/<owner>/skills/`, including the catalog and revisions. Routine Harness upgrades retain that directory. For an explicitly new task, the selected retained knowledge manifest carries the declared skill files. Promote useful workspace lessons here after reviewing current applicability, consolidating overlaps and removing local paths and experiment-specific instructions. Increment the package version whenever its content or attachments change.

See [input management](../docs/agent-inputs.md) and [project structure](../docs/project-structure.md).
