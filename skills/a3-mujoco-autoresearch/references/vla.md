# Vision–language–action adaptation

Checked 2026-11-02. Mechanisms below are source-established; A3 priorities and recipes are engineering hypotheses. See [project contract](project-contract.md) before translating another embodiment.

## Distinguish the families

| Method | Relevant mechanism | A3 decision |
|---|---|---|
| SmolVLA | Approximately 450M parameters, multi-view/state/instruction conditioning and flow action chunks | Existing A3 integration and moderate size make it a useful first VLA branch, not a guaranteed winner |
| Original OpenVLA | 7B pretrained VLA with discrete action-token prediction | Useful transfer reference; don't confuse its behavior with continuous chunk variants |
| OpenVLA-OFT | Parallel continuous action-chunk prediction and L1 objective, including ALOHA evidence | Potential bimanual transfer, but separate adapter/compute effort |
| π0 | Pretrained VLM plus continuous flow-matching action expert | Adapt an approved compatible checkpoint; not foundation pretraining |
| π0.5 | Broader heterogeneous co-training and semantic/generalization capabilities | A small A3 finetune does not reproduce its pretraining mixture or prove broad language generalization |

Sources: [SmolVLA paper](https://arxiv.org/abs/2506.01844), [model card](https://huggingface.co/lerobot/smolvla_base), [OpenVLA paper](https://arxiv.org/abs/2406.09246), [OFT](https://arxiv.org/abs/2502.19645), [π0](https://arxiv.org/abs/2410.24164), [π0.5](https://arxiv.org/abs/2504.16054).

## SmolVLA practical branch

Start with the already supported checkpoint/data interface where available. Inspect physical feature dimensions, max padded dimensions, processor tensors, camera keys, language length, chunk length and action-output slicing. Compatible8→16 capacity can reuse projections but requires actual right-arm supervision; see project contract. Never interpret pretrained unused channels as a trained A3 right-arm policy.

First establish synchronous inference and an exact fresh-process checkpoint reload. Match ACT's data, scene split and evaluation conditions when the scientific question is VLA transfer. Record actual trainability settings: a fully loaded VLM checkpoint plus action expert is not equivalent to random initialization. Tiny-subset loss reduction checks plumbing, not robot competence.

Useful controlled candidates:

- Execution prefix versus contact-feedback latency, holding weights fixed.
- Camera coverage/crop and task-relevant pixel detail.
- Dataset balancing and recovery examples at matched exposure.
- Trainable parameter subset/LoRA where the actual implementation supports it, holding base revision and normalization fixed.
- Instruction dependence in genuinely ambiguous tasks with paired prompts and matched scenes. Constant-language data cannot establish language following.

The official roughly 50-demo / 20k-step examples are feasibility illustrations, not A3 sufficiency laws or time estimates. Profile real memory/throughput. Separate microbatch, accumulation, effective batch and optimizer updates. A3's inference `num_steps` controls the flow sampler; don't confuse it with training steps.

Only add asynchronous execution after measured latency justifies it. Log observation/action timestamps, queue age, committed prefix and episode-reset cancellation. Compare success and wall-clock behavior; nominal policy throughput alone can conceal stale actions.

## Larger VLA branches

[OpenVLA repository](https://github.com/openvla/openvla) and [OFT implementation](https://github.com/moojink/openvla-oft) provide transfer recipes, not A3 adapters. OFT's ALOHA evidence is related-embodiment evidence. Check camera/proprio inputs, action normalization key, separate action-head/adapter weights and exact restore. Full-model versus LoRA, quantized inference versus training and multiview activations have different memory costs; a model fitting inference on 24GB does not establish training feasibility. Compare compute as well as task score, and defer a branch rather than silently obtaining more resources.

[OpenPI](https://github.com/Physical-Intelligence/openpi) supports different model/head paths including π0-FAST and flow variants. Treat switching head/representation as an architectural change. Preserve checkpoint-specific transforms, absolute/delta conventions and padded dimensions. The source's indicative >8GB inference, >22.5GB LoRA and >70GB full adaptation figures depend on its stack; profile the actual branch. Do not turn the figures into promises or reservations.

[Knowledge insulation](https://www.pi.website/research/knowledge_insulation) is a representation/objective/gradient-flow design, not merely “freeze all VLM weights.” Reproduce only the parts explicitly supported by the selected implementation.

**A3 π0.5 mismatch:** pinned code currently restricts benchmark task/prompt, and full scene alignment changes evaluation geometry. Reproducing the provider's own scene can diagnose a checkpoint but must be labeled separately. A fair phase-two candidate needs an adapter compatible with the fixed observation/action/task protocol; don't change the scene to claim a gain.

## Task text throughout training and inference

Pass the actual declared task instruction into the language-conditioned model in both training and inference. Trace dataset text to tokenizer tokens/mask and model forward, and benchmark text through the real runner and adapter to inference. Reject empty or unknown task conditions; do not replace the instruction with an empty string or leave it only in logs. Check task-distinguishing tokens survive truncation and both paths use matching task semantics, tokenizer assets, prompt construction and preprocessing. Exercise the actual runner and real model as well as synthetic tests. Fixed-observation prompt changes test the active condition path; matched behavioral evaluation tests whether the model learned to follow it.

## Data/transfer checklist

Keep immutable base revision, finetune lineage, task text, tokenizer, processor, train statistics and model hashes together. Detect prompt truncation, language shortcuts, camera permutation, gripper inversion and per-axis scale errors. Normalize once and invert once. An input dimension that fits a padded tensor still needs semantic alignment. Fine-tuning weights without processor state is an incomplete policy artifact.

Use offline cached assets under the run's actual permissions; installing/downloading model weights or uploading data is not implied by reading this skill. Verify code, base-weight and dataset licenses separately. Code-license labels alone do not settle all inherited weight terms.

Failure prioritization: immediate divergence→adapter/time/normalizer; good approach but failed contact→prefix/visual detail/action coverage; instruction ignored→conditioning/data ambiguity; brittle new layout→coverage and visual shortcuts; memory exhaustion→profile activation/batch/precision before replacing the entire method.
