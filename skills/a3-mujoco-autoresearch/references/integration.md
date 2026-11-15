# Skill discovery and controlled updates

The project uses one root `skills/` library. Every method has the same management interface and permissions, with its source recorded as provenance. Packages contain `SKILL.md`, `assets/catalog-entry.json` and optional resources. The Host configures `A3_SKILLS_ROOT`; no package is installed into the research workspace or frozen with the three prompts.

## Read only what the question needs

Start with `skills(action="list", query="...")` to inspect names, summaries and versions. Select a matching method with `skills(action="read", id="...")`. Both discovery and text reads support paging.

Use `list_resources` to find relevant references, templates or helpers, then `read_resource` with the skill ID and resource path. These are actions of the same `skills` tool. Ordinary file tools do not access the library. Keep unrelated bodies and attachments out of context.

## Create, revise and archive

`save` creates a method from its ID, description, content, source and optional keywords. Read an existing body before changing it and provide its returned `expected_sha256`. Resource updates use the resource's own hash: `write_resource` creates or replaces text attachments, and `delete_resource` removes them. A missing attachment can be created without a hash; replacing or deleting an existing one requires its current hash.

`archive` hides an outdated method from ordinary discovery while preserving the package. Use `list(include_archived=true)` to find it, and `save` to restore it. All mutations increment the package version and preserve the previous complete package under Host-managed `.runtime/skill-revisions/`. Re-read after a revision conflict before merging changes.

Skills and `memory/entries.jsonl` persist independently of Harness releases. Save specific findings through `memory`, and reusable methods through `skills`. Inspect helpers before adapting them into `workspace/research/` and submitting them to a remote job. Skill access does not execute code or expand file permissions.
