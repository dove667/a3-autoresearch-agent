# Data, training and control diagnostics

These are evidence-guided probes, not a fixed ritual to repeat every run. Choose the cheapest observation that distinguishes the current hypotheses. Sources checked 2026-11-02: [robomimic datasets](https://robomimic.github.io/docs/tutorials/dataset_contents.html), [sequence dataset](https://robomimic.github.io/docs/modules/dataset.html), [PyTorch AMP](https://docs.pytorch.org/docs/stable/notes/amp_examples.html), [checkpointing](https://docs.pytorch.org/tutorials/beginner/saving_loading_models.html), [reproducibility](https://docs.pytorch.org/docs/stable/notes/randomness.html), [MuJoCo state](https://mujoco.readthedocs.io/en/stable/programming/simulation.html). Pin APIs to installed versions.

## Data before capacity

Inventory trajectory count/length, task/layout coverage, camera completeness, action ranges, holds, failures and recoveries. Split entire episodes and related collection/scenario groups before constructing temporal windows or fitting statistics. Near-duplicate demonstrations crossing splits invalidate independence; adjacent-frame splits leak almost identical chunks.

Plot synchronized state, predicted/applied targets and images at approach, grasp, lift/transfer, release and termination. Check RGB/BGR, HWC/CHW, value range, camera order and time alignment. Distinguish action playback (controller validity) from state playback (scene inspection). A visually plausible replay of states doesn't prove recorded actions work.

Use training-only normalization and round-trip tests. Log near-constant dimensions, clipping frequency and values outside train support. Action chunk padding must not cross episodes; test start/end indices and masks with synthetic short episodes. Action-dimension padding for pretrained models is a different axis from terminal time padding. Check camera augmentations for temporal/geometric consistency and whether critical fingers/edges survive cropping.

Treat filtering as reversible masks with reasons. Removing all failed/slow trajectories can remove recovery behavior and difficult cases. Balance phases only when the data support reliable phase labels; compare at matched exposure. Don't call repeated held-out evaluation “untouched test” after using it for targeted fixes.

## Optimization probes

A tiny-batch fit answers whether the learning path can represent and optimize examples. It doesn't prove generalization. If it fails: inspect target scale, valid mask, gradient connectivity, frozen parameters, augmentation and indexing before widening the network.

Log actual optimizer updates, examples processed, each loss component, per-component errors, LR groups, preclip gradient norm/clip fraction, nonfinite tensors, scaler-skipped updates, memory and input/compute wait. A scalar total loss can hide gripper errors or KL dominance. Loss improvement is diagnostic; choose policies with closed-loop results.

For mixed precision, unscale before clipping/inspection. During accumulation, keep scale consistent across the effective batch and update optimizer/scaler at that granularity. Autocast doesn't mean every operation must be half precision. Compare suspected numerical issues with a controlled FP32 region/run, not unexplained multiple changes. Scheduler semantics must track actual completed updates, including skipped steps and tail batches.

Increasing accumulation preserves neither identical examples/order nor exact numerical behavior automatically. Record effective batch and compare both matched-work and matched-time learning. Low GPU utilization may be image decode/dataloader/simulation/serialization wait, not insufficient model size.

## Measure the actual experimental cost

Do not infer the critical path from GPU utilization or model size. Use existing telemetry or a bounded representative sample to separate reset/startup, physics, rendering, policy inference, serialization, disk I/O, collection acceptance rate and training. Report actual wall time and controls/second or completed episodes/hour with task, horizon, worker count, effective CPU allocation and concurrency. A short rollout can underestimate long 2×10 episodes or overestimate performance by measuring only startup; give a range and assumptions rather than a precise unsupported full-run estimate.

When a throughput probe already establishes that a full job will be impractical, preserve the partial evidence and estimate remaining time instead of finishing merely to obtain a benchmark number. A canceled probe remains partial; do not convert it into a completed scientific comparison. This is a targeted diagnostic when useful, not mandatory profiling before every experiment.

Reuse compatible collected trajectories and matching incumbent evaluations. Add data where a hypothesis requires new coverage rather than recollecting the entire set. Development can use declared paired subsets; the final full-grid requirement and missing-episode accounting remain unchanged.

Compare worker counts, batching and scheduling by measured completed-work throughput, not nominal parallelism. Record actual CPU allocation and concurrent workload so a hardware or contention change is not mistaken for an algorithmic improvement. Prefer a change that reduces the measured critical path while preserving task conditions and result quality.

## Resume and artifact integrity

Weights-only warm start is not exact training continuation. A continuation receipt can include optimizer/scheduler/scaler/EMA state, RNGs, data-sampler position, global step, processors, config and code/data hashes. Multiworker prefetch state makes exact mid-epoch continuation nontrivial: resume at a documented reproducible boundary or state the limitation. Check fresh-process reload outputs before a long job.

MuJoCo replay may require more than qpos/qvel: integration state, warmstart, controller/task/RNG state and assets/version matter. Preserve the actual state contract rather than promising bitwise replay from a partial vector. Never load untrusted pickle checkpoints merely to inspect metadata.

## Diagnostic map

| Observation | Discriminating probe | Candidate next action |
|---|---|---|
| Low teacher-forced loss, immediate failure | First action before/after processor and environment; timing; arm hold | Fix verified contract mismatch before more training |
| Approach succeeds, pinch/release fails | Gripper phase errors, occlusion, action age, clipping | Prefix or visual-detail/data-coverage experiment |
| Inter-arm handoff fails | Joint timelines and contact state under declared observations | Genuine coordinated data/joint modeling, not duplicate left actions |
| Chunk-boundary jumps | Raw target discontinuity, sampler randomness, stale queue | Same-weight M/aggregation test; then timing-aware integration |
| Later checkpoints worse | Fixed development rollouts, episode overlap, memorization | Earlier selection or data/regularization comparison |
| NaNs/random catastrophes | Gradients, masks, scaler skips, physical scales | Isolate numerical issue before architecture changes |
| High cost, little progress | Examples/s, optimizer/s, dataloader and wait breakdown | Reversible throughput fix with preserved semantics |
| Count high but complete success low | Release/stability/boundary/slot unmet conditions | Diagnose the predicate; don't redefine it |

The phase taxonomy is descriptive; use existing observable signals, and mark unknown rather than inferring hidden events from the final count alone.
