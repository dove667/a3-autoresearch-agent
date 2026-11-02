# Current validation: version 0.1.4

Prepared from repository `6bde3b5` on 2026-11-02. The helper accepts protocol 0.3.0 and reference-only 0.3.1, requires matching version and digests in a pair, rejects unknown versions and reports the actual input version. Tests show identical scientific outputs for identical fixtures under either supported version. This is offline helper evidence, not a new policy result or live skill adoption. Earlier integration evidence below is historical and was not rerun as a live Agent test. Full offline repository suite: 590 passed; Ruff, whitespace, skill metadata and relative links pass.

# Validation record for version 0.1.0

Authored and checked 2026-11-02 against parent repository commit 401edc42b366703a6e2be4925fc947c1fcbad4e9. This record establishes offline package behavior, not A3 policy efficacy or live Agent adoption. Revalidate relevant checks after edits.

- Repository suite: **562 passed** (including 24 skill-helper tests), Python 3.11 cloud review environment, `MSWEA_GLOBAL_CONFIG_DIR=/tmp/a3-mini-config`, `LITELLM_LOCAL_MODEL_COST_MAP=True`, `MSWEA_SILENT_STARTUP=1`.
- Ruff and git diff whitespace checks passed.
- Skill frontmatter/naming validator passed; all package-relative Markdown references resolve.
- Helper fixtures exercise normalized score/paired differences, seed-cluster preservation, full/partial coverage, malformed/duplicate/missing data, fixed-condition/protocol mismatch, single-seed uncertainty, raw-schema rejection and read-only CLI behavior. Source observations and simulator results were not generated.

## Actual F interface isolation check

Read-only reference: ready retained run `run-20261102-182101-579869`, version `version-97dba45d8fb245c3914b0663784fcf9c`, retained commit `30cf94949073f172dd005aa59a04e3c8768d83ee`.

- F skill_tools.py SHA256: `aba683ccb97fb0c92d949dff9e4f3a7187e1c956f3614f5f4150e4238871c9fc`.
- Original catalog SHA256: `557da2941c58064d30e4422ff21a5e1a71a2669376fee33039dfe1416be90910`.
- In a temporary copy, merged the supplied catalog entry and copied this skill directory. Actual F dispatch discovered the skill, returned its local-source metadata and permitted ordinary file reading of the body. Unsupported skill_execute correctly refused execution. The existing synthetic checker retained its local-function status. Original source catalog hash remained unchanged.

No F source, historical checkpoint, private dialogue or secret is included in this package. No active run, retained source, model, dataset or runtime permissions changed. The isolation test is not evidence that a model chose the skill, understood every reference, or executed a robot experiment.

Independent read-only scenario review covered six realistic requests: 8D-to-16D adaptation, ACT loss versus long open-loop execution, incomplete evaluation, world-model prerequisites, skill discovery/execution/retention boundaries, and pi0.5 scene-override comparability. Responses followed the intended boundaries; review prompted an explicit metric formula and worked raw-to-normalized mapping. This is qualitative instruction-use evidence, not a trained-policy result or a guarantee of future Agent behavior.

Version 0.1.1 introduced documentation about conditional pipeline bottlenecks, bounded throughput estimates, valid reuse and Host quota boundaries. Version 0.1.2 removes the Host resource-administration guidance as outside the research skill's audience, while retaining experimental-efficiency methods without assuming which stage dominates. Neither revision changes helper code or claims measured throughput or a live experiment. Earlier helper-test evidence applies to the unchanged helper; documentation revisions receive separate Markdown-link, metadata and whitespace checks.
