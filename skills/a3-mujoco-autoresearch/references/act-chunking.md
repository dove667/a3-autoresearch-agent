# ACT and action chunking

Evidence: original ACT [paper](https://arxiv.org/abs/2304.13705), [implementation](https://github.com/tonyzhaozh/act), [LeRobot 0.5.1 implementation](https://github.com/huggingface/lerobot/blob/v0.5.1/src/lerobot/policies/act/modeling_act.py), and [A3 ACT workflow](https://github.com/Eter0109/a3_dual_arm_sim/blob/6ff458b0b73b78a3ffd794e8b37fdf297a23af48/docs/act.md). Checked 2026-11-02. Recipes below are hypotheses, not A3 improvement results.

## Mechanism and what transfers

ACT predicts a multi-step joint-action chunk conditioned on images/proprioception with a transformer and a conditional variational training objective. Reconstruction and KL terms have different roles. The original deployment uses the zero latent; the presence of a CVAE does **not** mean that it samples diverse strategies online. The original implementation has 14D ALOHA assumptions: A3 requires its own verified 8D/16D interface. ACT's A3 adapter ignores language, so identical visual/state inputs with different instructions cannot establish instruction following.

A3's documented starting configuration is 20k optimizer updates, batch 8, K=50, executed prefix M=8, learning rate 1e-5, ImageNet ResNet18 and MEAN_STD preprocessing. These are a reproducible project baseline, not recommended universal optima or a claim that the present run used them. Read installed config and data revision before reuse.

## Separate three temporal variables

- K: number of predicted actions. At 20 Hz K=50 spans 2.5 s.
- M: number consumed before replanning. M=8 runs open-loop for 0.4 s.
- Aggregation: combine predictions made at different times for the same current action. It is neither EMA model weights nor simply executing a larger chunk.

Begin with a same-checkpoint M ablation if failures suggest stale feedback. Compare a shorter supported prefix near the critical correction timescale, measuring p50/p95 inference latency and action age. At M=1 the feedback is freshest but inference demand greatest. A3/LeRobot ensembling requires M=1. Then, separately, retrain a changed K if insufficient plan context or padded far-future targets appear limiting. K changes supervised target count and padding, so equal updates alone are not equal work.

**Counterintuitive pinned behavior:** LeRobot 0.5.1 uses exp(-coefficient × age-index) where index zero is the oldest prediction. A positive coefficient emphasizes older predictions, negative newer. Test with deliberately distinct old/new values before interpreting smoothing. Original ACT's nonzero-action sentinel can discard legitimate zeros; an independently written adapter should use explicit validity masks rather than copying it.

LeRobot's masked reconstruction uses a whole-tensor mean rather than normalizing solely by valid targets. When K changes, log valid fraction and effective reduction; don't silently alter reduction while claiming a pure horizon experiment. Do not copy the original real-robot loader's action-offset heuristic into A3: the recorder's actual timing governs alignment.

## Useful experiments and diagnostic decisions

1. **Incumbent reproduction:** same dataset/processor/cameras/config, exact reload and a short valid rollout. A smoke checkpoint is not a trained incumbent.
2. **Execution horizon:** same checkpoint, change M only; inspect approach, pinch, lift and release errors. If only contact corrections improve, report that mechanism rather than a general architectural win.
3. **Perception:** compare all cameras with a task-motivated subset, preserving crops and training exposure. Check fingers/cookie edges survive preprocessing. Freezing the visual backbone is an explicit compute/adaptation tradeoff.
4. **Objective:** log reconstruction and KL separately, per-component errors in normalized and physical units, latent sensitivity. Near-zero KL alone doesn't prove a harmful collapse; compare behavior. VAE-off is a different objective, not a harmless speed switch.
5. **Coordination:** genuine bimanual data and jointly conditioned 16D targets are required for bimanual learning. Duplicating left-arm targets or training unrelated arm heads is not proof of coordinated control.

For apparently low loss but immediate failure, inspect processor inversion, arm ordering, inactive-arm hold and time alignment before training longer. For smooth yet unresponsive behavior, inspect old-prediction weighting and M. For chunk-boundary jerks, compare target discontinuities and queue age. A new episode must clear queues/ensembles and invalidate old inference results.

## Latency-aware extensions

Synchronous correctness comes first. Measure capture, preprocessing, inference with GPU synchronization, postprocessing and action consumption separately. Simulator time can pause during inference: successful simulated rollouts do not establish 20 Hz real-time performance.

If measured latency warrants asynchronous work, specify observation timestamps, committed action prefix, queue replacement and reset cancellation. [Real-Time Chunking](https://arxiv.org/abs/2506.07339) uses frozen committed actions and inpainting for diffusion/flow policies; a background thread or averaging alone is not RTC. Treat such changes as separate deployment experiments.
