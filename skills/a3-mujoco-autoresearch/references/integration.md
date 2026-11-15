# Skill discovery and retention

The Host snapshots complete reviewed packages from the repository's `skills/` directory together with the three files in `prompt/`. SOUL provides the long-term A3 research goal. Each skill package has a `SKILL.md` entrypoint and `assets/catalog-entry.json`; the catalog provides its identity and semantic version. Every delivered file receives that version and its own SHA256.

## Read and refine

At Agent initialization, `install_input_skills` installs each frozen tree into the shared project `workspace/` and registers it in `workspace/skills/index.json`. Discover relevant entries with `skills(action="list", query="...")`, then use `skills(action="read", id="...")` for the actual body. Read only the references needed for the research question with the ordinary file tool.

Use `skills(action="save", ...)` to register an Agent-created or improved text skill with its applicability, sources and keywords. Updating an existing entry requires the current body SHA256 from `read`. The tool saves revisions and updates the catalog. `archive` hides an entry from default discovery while keeping its history readable.

The registry manages text. Execute a helper script only through an authorized job tool, after inspecting its actual interface and dependencies. Binary datasets and weights use authorized exports.

## Keep knowledge across maintenance

Future sessions and Harness upgrades use the same root `workspace/` and `memory/entries.jsonl`. The skill catalog, bodies, attachments and revisions stay in `workspace/skills/`. Installing an unchanged deployed tree preserves the Agent's local edits. A changed deployed package receives a separate reference identity when the original ID already exists; the Agent can compare and reconcile it.

Publishing a repository change takes effect through an explicit input update or an offline Harness release. Skill snapshots record the catalog, bodies, necessary references and helpers; ordinary recovery continues using the installed knowledge.

Promote reusable workspace lessons into the repository after reviewing current interfaces, consolidating overlapping guidance and removing local paths and experiment-specific instructions. Increment the catalog and frontmatter version for any change to a package or its attachments.

Use the skills tool to discover other installed research skills. The repository package index and input management documentation describe publication and snapshots.
