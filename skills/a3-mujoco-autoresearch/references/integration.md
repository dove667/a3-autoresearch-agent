# Skill discovery and retention

The Host snapshots complete reviewed packages from the repository's `skills/` directory with the task inputs. Each package has a `SKILL.md` entrypoint and `assets/catalog-entry.json`; the catalog provides its identity and semantic version. Every delivered file receives that version and its own SHA256.

## Read and refine

At Agent initialization, `install_input_skills` installs each frozen tree into the current workspace and registers it in `skills/index.json`. Discover relevant entries with `skills(action="list", query="...")`, then use `skills(action="read", id="...")` for the actual body. Read only the references needed for the task with the ordinary file tool.

Use `skills(action="save", ...)` to register an Agent-created or improved text skill with its applicability, sources and keywords. Updating an existing entry requires the current body SHA256 from `read`. The tool saves revisions and updates the catalog. `archive` hides an entry from default discovery while keeping its history readable.

The registry manages text. Execute a helper script only through an authorized job tool, after inspecting its actual interface and dependencies. Binary datasets and weights use authorized exports.

## Keep knowledge across maintenance

Routine Harness upgrades reuse the same research workspace, including its catalog, bodies, attachments and revisions. Installing an unchanged deployed tree preserves the Agent's local edits. A changed deployed package receives a separate reference identity when the original ID already exists; the Agent can compare and reconcile it.

For an explicitly new task, the selected retained knowledge manifest declares the skill files to carry forward. Include the catalog, bodies, necessary references and helpers. Publishing a repository change takes effect through a new task snapshot or an offline release of the existing task.

Promote reusable workspace lessons into the repository after reviewing current interfaces, consolidating overlapping guidance and removing local paths and experiment-specific instructions. Increment the catalog and frontmatter version for any change to a package or its attachments.

Use the skills tool to discover other installed research skills. The repository package index and input management documentation describe publication and snapshots.
