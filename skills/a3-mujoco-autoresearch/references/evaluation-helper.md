# Offline paired evaluation helper

Run `python skills/a3-mujoco-autoresearch/scripts/compare_evaluations.py baseline.json candidate.json` only through an available authorized execution tool. It reads files and prints JSON; no simulator, model, network, GPU or source-file modification. Default bootstrap uses 10,000 deterministic paired resamples; `--bootstrap-seed` and `--bootstrap-repeats` control analysis only, not experimental seeds.

## Explicit normalized schema

Each input has:

- `schema`: `a3-paired-evaluation-v1`
- `protocol_version`: `0.3.0` through `0.3.6`; baseline and candidate must use the same version and protocol digest
- `protocol_sha256`: digest of the actual saved scientific protocol.
- `conditions_sha256`: digest of a canonical, independently checked manifest of A3 version, physics/randomization/success config, observation/control interface, task prompts, scene/source settings, dataset split, seed plan and external evaluation conditions. Exclude the policy treatment itself; preserve it separately. Same digest is necessary but does not prove the manifest truthful.
- `policy_snapshot_sha256`: digest of the full policy snapshot manifest (weights, processor, code, inference settings and declared task routing), not only a weights filename.
- `split`: `development` or `final-test`.
- `episodes`: objects with `task` (`2x5`/`2x10`), `difficulty` (`basic`/`medium`/`advanced`), `seed`, `inference_seed`, `status`, `cookies_in_target`, `success`, `max_steps`, `control_hz`.

Integers must be integers, not booleans/floats. Each key `(task,difficulty,seed)` must appear once and match the other report. Inference seed must match scenario seed. Counts must be final integer counts within 10/20; success remains the original Boolean predicate, not inferred from count. The helper refuses noncompleted episodes, duplicates, mismatched conditions, unknown protocol and missing pairs. Do not remove crashed episodes to make the inputs pass: diagnose/recover the original job or report an invalid/incomplete comparison.

## Adapt raw A3 output deliberately

Inspected A3 source at `6ff458b0b73b78a3ffd794e8b37fdf297a23af48`:

- Single benchmark: `{summary, episodes, run}`. Episode records include `seed`, `cookies_in_target`, `success`, `steps`, `failure_reason` and other diagnostics; task/profile/max_steps come from `run`.
- Grid `episodes.jsonl` adds `benchmark`, `profile`, `max_steps`, `prompt_style`, `task_instruction` to each score. Grid `summary.json` is an aggregate, not a substitute for episode records. `protocol.json` provides additional conditions.
- Expert `--compare` output has named `same_column` and `cross_column` branches. Do not combine them or treat them as learned-policy candidates.

The supplied helper intentionally does **not** infer missing inference seed, control frequency, policy hashes or completion from raw scores. Adapt verified raw records plus the actual job/protocol manifest. Preserve a mapping to every source file/record and source SHA256. An adapter should fail when fields are unknown, not manufacture defaults from README examples. `steps` is actual steps consumed, not the configured `max_steps`. A genuine completed task failure can have count0; a crashed/missing episode cannot be relabeled count0.

## Output and limitations

Cell results include paired means/counts, complete-success counts and conditional bootstrap intervals. Formal `G` is emitted only for all six cells sharing exactly 20 scenario seeds (120 episodes) at these protocol revisions. Partial runs emit cell results but no formal G. For full G, each bootstrap draw resamples a seed with both tasks/all difficulties together, preserving within-scenario correlation. It is a percentile interval, not a claim of BCa accuracy.

With fewer than two scenarios, cell interval is null. Constant samples can produce zero-width empirical bootstrap intervals; that does not demonstrate zero population uncertainty. Any interval is conditional on these scenarios and one trained snapshot; it cannot establish training-seed robustness or undo adaptive test leakage. Multiple-training-run hierarchical analysis is outside this helper. An interval excluding zero can indicate decline; check sign and practical effect, not just exclusion. No adoption decision or acceptance pass is generated.

The helper validates **structure and stated comparability**, not authenticity. `provenance_verified:false` remains explicit. Recompute and inspect referenced files separately. Offline fixture tests are not local robotic rollout evidence.

## Worked mapping example (handcrafted, not an observed run)

A single-cell source record can have this inspected A3 shape:

```json
{
  "run": {"task": "2x5", "profile": "basic", "max_steps": 500},
  "episodes": [{"seed": 10000, "cookies_in_target": 4, "success": false, "steps": 500}]
}
```

After independently checking the job completion receipt and saved evaluation manifest establish `status=completed`, `inference_seed=10000`, `control_hz=20`, this one record maps to:

```json
{
  "task": "2x5", "difficulty": "basic", "seed": 10000,
  "inference_seed": 10000, "status": "completed",
  "cookies_in_target": 4, "success": false,
  "max_steps": 500, "control_hz": 20
}
```

The mapper takes task/difficulty/max_steps from run metadata, count/success/seed from the episode, and completion/inference/control evidence from the saved job/protocol receipt. It does **not** assume those latter facts from this partial raw JSON. For grid records, map `benchmark→task` and `profile→difficulty` instead. Store the raw file hash and row reference in the adaptation receipt, compute the report-level protocol/conditions/policy-manifest hashes from actual saved files, and verify all planned episodes before interpreting completeness. This one row is partial evidence and cannot produce a full-grid score.

For a complete canonical grid, the exact scalar is:

    G = (1/6) * sum_over_six_cells(mean_final_cookies_in_target / task_capacity)
    task_capacity = 10 for 2x5; 20 for 2x10
    paired_delta_G = G(candidate) - G(baseline)

Cells receive equal weight, not an unnormalized sum of cookies across tasks. The bootstrap keeps all six cells for each sampled scenario seed together. The fixed finite grid score is exact for those records; its interval additionally treats those scenarios as representative samples of a broader condition distribution.
