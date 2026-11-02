# Sources and evidence boundaries

Reviewed 2026-11-02. References are primary papers, author repositories, official framework documentation or the supplied project brief. Recommendations in this skill are original synthesis, not quotations or verified local improvements. Moving `main`/documentation/model-card URLs must be pinned again before reproducing a specific implementation.

## Project authority

- Supplied `midterm(2).pdf`, three-page course brief: method families and research workflow; not the phase-two runtime contract.
- [A3 source snapshot6ff458b](https://github.com/Eter0109/a3_dual_arm_sim/tree/6ff458b0b73b78a3ffd794e8b37fdf297a23af48): contracts, adapters, dataset/training/evaluation surfaces inspected. This is not evidence of server deployment or local assets.
- LeRobot 0.5.1, commit1396b9fab7aecddd10006c33c47a487ffdcb54b4: [ACT](https://github.com/huggingface/lerobot/blob/1396b9fab7aecddd10006c33c47a487ffdcb54b4/src/lerobot/policies/act/modeling_act.py), [SmolVLA](https://github.com/huggingface/lerobot/blob/1396b9fab7aecddd10006c33c47a487ffdcb54b4/src/lerobot/policies/smolvla/modeling_smolvla.py), [diffusion](https://github.com/huggingface/lerobot/tree/v0.5.1/src/lerobot/policies/diffusion).
- RSI agent-inputs/phase-two/protocol.md 0.3.0 and SOUL.md 0.1.0 at source 401edc4. Active run snapshots always take precedence over this historical reference.

## Method mechanisms

- ACT/ALOHA: [2304.13705](https://arxiv.org/abs/2304.13705), [author code](https://github.com/tonyzhaozh/act).
- Diffusion Policy: [2303.04137](https://arxiv.org/abs/2303.04137), [author project](https://diffusion-policy.cs.columbia.edu/), [code](https://github.com/real-stanford/diffusion_policy).
- Flow Matching: [2210.02747](https://arxiv.org/abs/2210.02747). Mathematical objective, not evidence of A3 gains.
- SmolVLA: [2506.01844](https://arxiv.org/abs/2506.01844), [official docs](https://huggingface.co/docs/lerobot/smolvla), [base card/config](https://huggingface.co/lerobot/smolvla_base). Published base capacity doesn't establish dimensions of every Eter finetune.
- OpenVLA: [2406.09246](https://arxiv.org/abs/2406.09246), [code](https://github.com/openvla/openvla). OFT: [2502.19645](https://arxiv.org/abs/2502.19645), [code](https://github.com/moojink/openvla-oft).
- π0: [2410.24164](https://arxiv.org/abs/2410.24164). π0.5: [2504.16054](https://arxiv.org/abs/2504.16054). [OpenPI](https://github.com/Physical-Intelligence/openpi), [knowledge insulation](https://www.pi.website/research/knowledge_insulation). Head, weights and training stack must match; code license alone does not certify every checkpoint's terms.
- RTC: [2506.07339](https://arxiv.org/abs/2506.07339). A concrete latency-aware method, not a synonym for asynchronous execution.
- TD-MPC2: [2310.16828](https://arxiv.org/abs/2310.16828), [code](https://github.com/nicklashansen/tdmpc2).
- DreamerV3: [2301.04104](https://arxiv.org/abs/2301.04104), [code](https://github.com/danijar/dreamerv3).
- DINO-WM: [2411.04983](https://arxiv.org/abs/2411.04983), [code](https://github.com/gaoyuezhou/dino_wm). DINO-world: [2507.19468](https://arxiv.org/abs/2507.19468). Related visual prediction/planning results, not A3 validation.

## Research and engineering methods

- AIDE: [2502.13138](https://arxiv.org/abs/2502.13138), [code](https://github.com/WecoAI/aideml).
- MLAgentBench: [2310.03302](https://arxiv.org/abs/2310.03302), [code](https://github.com/snap-stanford/MLAgentBench).
- Reliable RL evaluation: [2108.13264](https://arxiv.org/abs/2108.13264), [rliable](https://github.com/google-research/rliable). Aggregate metrics are not substitutes for the project's fixed G.
- [robomimic dataset contents](https://robomimic.github.io/docs/tutorials/dataset_contents.html), [sequence dataset](https://robomimic.github.io/docs/modules/dataset.html): transferable inspection concepts, not a requirement to migrate data format.
- [PyTorch AMP](https://docs.pytorch.org/docs/stable/notes/amp_examples.html), [checkpointing](https://docs.pytorch.org/tutorials/beginner/saving_loading_models.html), [reproducibility](https://docs.pytorch.org/docs/stable/notes/randomness.html): pin local versions; exact repeatability across platforms not promised.
- [MuJoCo simulation state](https://mujoco.readthedocs.io/en/stable/programming/simulation.html): integration state is more than qpos/qvel.
- [SciPy bootstrap](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.bootstrap.html): pairing/degenerate-sample cautions. Included helper uses a transparent percentile bootstrap, not SciPy/BCa.

## What has not been established

No policy was trained, downloaded or evaluated to author this package. No local hardware feasibility or performance superiority is claimed. F catalog compatibility and offline helper fixtures are separate validation claims documented in integration notes. This package imports no third-party implementation code; linked repositories remain under their own licenses. Future copied/adapted code must retain applicable license notices and provenance.
