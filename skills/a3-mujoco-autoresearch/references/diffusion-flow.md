# Diffusion and flow-matching action policies

Sources checked 2026-11-02: [Diffusion Policy project/paper](https://diffusion-policy.cs.columbia.edu/), [paper](https://arxiv.org/abs/2303.04137), [author implementation](https://github.com/real-stanford/diffusion_policy), [LeRobot v0.5.1 config](https://github.com/huggingface/lerobot/blob/v0.5.1/src/lerobot/policies/diffusion/configuration_diffusion.py), [model](https://github.com/huggingface/lerobot/blob/v0.5.1/src/lerobot/policies/diffusion/modeling_diffusion.py), [Flow Matching](https://arxiv.org/abs/2210.02747), [π0](https://arxiv.org/abs/2410.24164). No A3 Diffusion trainer/CLI was verified. Integration is a proposed experiment, not an available command.

## Choose the hypothesis

Diffusion predicts a conditional action sequence through iterative denoising, followed by receding-horizon execution. It can represent multiple valid trajectories rather than their potentially invalid mean. Flow matching regresses a conditional vector field and integrates an ODE from noise to actions; it is not inherently a VLA or one-step policy. The relevant hypothesis might be multimodal grasp/path ambiguity, smoother correlated chunks, or a better quality–latency tradeoff. Do not simultaneously swap encoder, data, objective and pretrained foundation and attribute the gain to diffusion alone.

Inspect demonstration strategy clusters and conditional ambiguity first. If actions differ because an unobserved instruction or phase matters, adding the missing valid conditioning may be more direct than a more expressive generator. Never average sampled incompatible strategies to create a nominally smooth action.

## Diffusion integration recipe

- Preserve the A3 observation/action/controller contract. Start with a supported temporal U-Net and matched visual encoder/data split. A transformer denoiser is a separate architecture branch.
- Pinned LeRobot defaults (observation history 2, horizon 16, execution 8) are PushT-oriented reference values. Translate to seconds and inspect alignment: execution may begin at `n_obs_steps-1`, so horizon is not necessarily the number of future executable actions. Prove window and slice indices with synthetic episodes, including reset and terminal padding.
- Pinned U-Net horizon must respect the architecture's downsampling divisibility constraint. A desired physical horizon still has to be representable.
- Use the model's required normalization. Pinned DP state/action defaults use MIN_MAX, clipping around [-1,1]; ACT z-scores cannot be substituted silently. Report clipping and out-of-training-range target frequency. Keep action-dimension padding separate from terminal time padding.
- Establish scheduler-compatible inference quality before reducing sampling steps. DDPM→DDIM is a named change; reducing training noise steps is not a substitute for correctly accelerating inference.
- Verify EMA support in the actual trainer and save/restore the evaluated EMA weights. Model EMA is different from action smoothing. Author configs warn about BatchNorm with EMA; inspect the actual encoder normalization before copying settings.
- Log total trainable parameters, GPU time, examples processed, peak VRAM and inference p50/p95. Equal update counts are not equal compute across ACT and diffusion.
- Pair inference seeds as the protocol specifies; don't retrospectively select the best sampled rollout. Extra seeds estimate inference stochasticity, not training-run variability.

Useful controlled comparisons: observation history one versus two at verified time spacing; same checkpoint execution prefix; scheduler/steps at fixed weights; camera/encoder choices; then denoiser capacity. Choose based on observed uncertainty, not a mandatory sweep.

## Compact flow baseline, if justified

A small conditional action-flow model can isolate the objective without requiring a large VLM. For normalized target chunk A and Gaussian Z, one explicit convention is:

    x(t) = (1-t) Z + t A,  t in [0,1]
    target velocity = A - Z

Train `v(x,t,observation)` with valid-action masking and integrate from t=0 to 1. This is a proposed simple flow baseline, **not** a reproduction of π0. Test constant velocity, endpoint integration, masks and conditioning before rollouts.

[OpenPI π0 source](https://github.com/Physical-Intelligence/openpi/blob/main/src/openpi/models/pi0.py) uses the reverse time convention: `x=t*noise+(1-t)*actions`, velocity `noise-actions`, and negative integration steps from 1 toward 0. It also uses a nonuniform training-time distribution. Mixing one convention's target with the other's sampler can produce finite training loss but broken actions. Pin the implementation and test signs explicitly.

Vary solver steps only after endpoint tests and a closed-loop baseline. Observe endpoint saturation, chunk discontinuities, coherent sample diversity and latency. A smooth learned field doesn't guarantee collision-free trajectories. Distillation/one-step generation is another training intervention, not a free inference toggle.

## Failure-to-action map

- Low normalized loss + saturated targets: audit normalizer, clipping and unseen action ranges.
- Excellent sampled chunks + bad rollout: inspect executed slice, history age, queues and environment action correction.
- Unstable gripper while arms smooth: inspect component scales, data imbalance and correlation; don't average independent samples per joint.
- Good quality only with slow sampler: characterize quality–latency curve; don't conceal paused simulator time.
- Weak diversity: inspect conditioning and demonstrations before increasing sampler noise.
- Divergent flow samples: verify time/sign/endpoint convention before changing model size.

All recipes require actual adapter validation and authorized execution. There is no verified `a3-sim train-diffusion` command in this skill.
