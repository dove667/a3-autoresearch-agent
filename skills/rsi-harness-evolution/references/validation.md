# Validation scope

Version 0.1.0, prepared 2026-11-02. This is reference guidance and an optional experiment-record template, not a runtime implementation or a measured improvement.

Source review: primary literature and official engineering documentation, plus the actual exported F module and repository execution/protocol contracts. Research examples were adapted into original conditional experiment cards, not copied code or promised results.

Checks completed on the tree based on `28aeef6`:

- Skill frontmatter validator passes for this skill and the updated A3 skill.
- JSON parses and all 13 relative Markdown links resolve.
- In a temporary copy of the actual exported F package: merge the catalog fragment, discover this entry, read its metadata and body, and verify its evidence paths. `skill_execute` refuses this reference as expected; the existing synthetic checker remains `local-function` and passes its synthetic fixture. Original export hashes are unchanged.
- Repository regression: **579 passed**; Ruff and `git diff --check` pass.

These checks do not demonstrate a live Agent using the skill or any local efficiency improvement.

Independent blind scenario review covered attribution, quality loss, recovery, unlisted solutions, candidate loading, context trust and task scope. It found the guidance useful and autonomy-preserving; two concrete gaps were corrected: explicit tool-output instruction laundering tests and backend-supported idempotency semantics. This is qualitative document evaluation, not a measured Agent performance trial. No model calls, robot training, live experiment, memory service installation or hot-loading is part of package preparation. A future modified copy must not inherit these checks without verifying affected behavior.
