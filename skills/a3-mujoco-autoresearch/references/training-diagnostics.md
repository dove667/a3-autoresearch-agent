# Data, training and control diagnostics

These are evidence-guided probes, not a fixed ritual to repeat every run. Select, adapt or skip them by expected information benefit, total implementation/validation cost and dependencies. Sources checked 2026-11-02: [robomimic datasets](https://robomimic.github.io/docs/tutorials/dataset_contents.html), [sequence dataset](https://robomimic.github.io/docs/modules/dataset.html), [PyTorch AMP](https://docs.pytorch.org/docs/stable/notes/amp_examples.html), [checkpointing](https://docs.pytorch.org/tutorials/beginner/saving_loading_models.html), [reproducibility](https://docs.pytorch.org/docs/stable/notes/randomness.html), [MuJoCo state](https://mujoco.readthedocs.io/en/stable/programming/simulation.html). Pin APIs to installed versions.

## Data before capacity

Inventory trajectory count/length, task/layout coverage, camera completeness, action ranges, holds, failures and recoveries. Split entire episodes and related collection/scenario groups before constructing temporal windows or fitting statistics. Near-duplicate demonstrations crossing splits invalidate independence; adjacent-frame splits leak almost identical chunks.

Plot synchronized state, predicted/applied targets and images at approach, grasp, lift/transfer, release and termination. Check RGB/BGR, HWC/CHW, value range, camera order and time alignment. Distinguish action playback (controller validity) from state playback (scene inspection). A visually plausible replay of states doesn't prove recorded actions work.

Use training-only normalization and round-trip tests. Log near-constant dimensions, clipping frequency and values outside train support. Action chunk padding must not cross episodes; test start/end indices and masks with synthetic short episodes. Action-dimension padding for pretrained models is a different axis from terminal time padding. Check camera augmentations for temporal/geometric consistency and whether critical fingers/edges survive cropping.

Treat filtering as reversible masks with reasons. Removing all failed/slow trajectories can remove recovery behavior and difficult cases. Balance phases only when the data support reliable phase labels; compare at matched exposure. Don't call repeated held-out evaluation “untouched test” after using it for targeted fixes.

## Visual process evidence

Prefer an original episode's existing video and trace when a grasp, transfer, placement or stagnation hypothesis needs visual evidence. Select useful step/time intervals rather than recording every rollout or using a fixed frame checklist. The current research Agent chooses the question, extracts frames in its authorized job, performs the diagnosis and evaluates findings. Final target count alone cannot localize failure. Missing action in sparse frames is not proof it never happened; report occlusion, sampling gaps and conflicting signals.

A3 source `7c34d38e58b6d94e76a9dec8b2b3bbc912263e38` provides `CookieBatchBenchmark.diagnostic_dir` and `EpisodeDiagnostic`. The native benchmark CLI accepts `--diagnostic-dir`, creates per-seed camera MP4s, `trace.jsonl`, `success_diagnostics.json`, and `result.json` on a normal return; it forces `workers=1`. The trace aligns before-action images/`observation_before` with distinct `raw_action`, `processed_action`, `applied_action` and after-step state. Its zero-based step indexes the video frame; derive simulation time from the actual control rate, not wall time. Native closing preserves collected diagnostics for unsuccessful episodes, whereas the ordinary dataset recorder can discard failures. Abrupt termination or I/O failure can leave incomplete files.

This diagnostic class does not produce state NPZ automatically. Separate expert scripts expose selected state-save options; those states are not videos or proof of exact replay. If an original episode lacks a recording, state that limitation. Any new rollout, action replay or state reconstruction must be labeled new diagnostic evidence with actual versions and conditions, never the missing original video. Diagnostic capture is optional and does not change the active scientific comparison or acceptance rules. Privileged contact/pose diagnostics must not silently become Policy inputs.

Saved-action replay can avoid new Policy inference when the original actions and compatible initial state are available. The native `a3-sim replay` reads parquet actions without video decoding and disables observation-camera rendering, but restores only its own limited seed/task contract; it does not recover arbitrary benchmark state or offer selected-frame capture. A separate authorized diagnostic driver can step every saved action with `render_cameras=False` and render only requested cameras/steps through MuJoCo's renderer. Verify version/reset/physics/control/action conventions and compare available saved states before treating frames as trajectory-consistent reconstruction. A helper called `ReplayPolicy` or `needs_images=False` is not a native interface in inspected A3 7c34d38. Do not skip intermediate dynamics, silently accept divergence or call reconstructed images original recordings.

Where the deployed Host offers `POST /vision/inspect`, the research Agent can submit one question and 1–6 chronological authorized-job PNG/JPEG references, including job/path, episode, step, simulation time and camera. The existing artifact reader only accesses the specified job's workspace: write frames to `/workspace/frames` and submit relative `frames/...` paths, never absolute `/outputs` paths. The current executor shares `/outputs` within one run/owner scope; a later CPU job in that scope can extract existing video into its own workspace, then reference its own job ID. Workspaces remain job-specific; same-run research identities can read each other's job artifacts; remote job reuse of large files uses the run's authorized sealed input exports. Cross-run materials require explicit import or authorization. Limits are 512 KiB per image, at most 1024 pixels per side and 3 MiB total. The Host validates and normalizes images without metadata, routes a tool-free visual helper and returns analysis with frame provenance and actual model. Main instructions and raw feedback are not automatically attached; images remain untrusted evidence. Base64 stays outside main/summary context. This endpoint neither downloads videos, reruns a Policy nor grants data access; generic gateway use still needs its loaded path allowlist and does not imply a dedicated tool is registered. Check the actual loaded interface before relying on it.

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
