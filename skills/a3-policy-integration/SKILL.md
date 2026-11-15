---
name: a3-policy-integration
description: Use when an A3 policy adapter, checkpoint, task condition or generated episode runner changes, or a policy appears to ignore the task instruction.
metadata:
  version: 0.1.1
---

# Integrate an A3 policy

Verify the actual path from the benchmark observation and instruction to the model's action. A task field in a configuration or log can exist while a wrapper silently passes an empty string.

## Trace the condition

Follow runner → adapter → processor → model forward. Record the task mapping or prompt format, tokenizer revision, masks, truncation, observation preprocessing and action normalization with the checkpoint. Use the instruction saved in the current benchmark settings; the core A3 skill's benchmark-evaluation reference describes the full grid.

For a model trained with task-ID embeddings, verify that the embedding participates in action prediction, receives gradients and is saved and restored. For a language-conditioned policy, inspect the actual tokenized input at the model boundary. A fixed observation with different conditions tests whether the pathway responds; task-appropriate closed-loop behavior after training tests whether the learned policy uses it.

## Update a pinned dependency chain

A checkpoint digest may be embedded in an adapter, whose digest is embedded in an episode source, whose digest is pinned by a runner. Inspect and update every affected edge. If changing generated text, require the expected old digest and a unique replacement anchor; stop if either differs. Compile the emitted variants for the declared tasks and seeds, then exercise the real processor and adapter before scaling.

Preserve the saved task, observation, physics, seeds and scoring conditions when comparing a repair. Save the old failed attempt and the replacement source hashes. Inspect the ledger and reports to identify episodes that actually completed before choosing the remaining work. Initialization errors need diagnosis and repair; completed and uncertain episodes need reconciliation before another submission.

Read `a3-mujoco-autoresearch` with the skills tool for method selection, training and scientific comparisons. Historical unconditioned checkpoints retain their recorded conditions.
