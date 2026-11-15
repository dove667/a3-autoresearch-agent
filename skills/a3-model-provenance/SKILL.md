---
name: a3-model-provenance
description: Use when registering a trained policy, reporting model comparisons or reconciling model names, intermediate checkpoints and historical experiment aliases.
metadata:
  version: 0.1.0
---

# Record model identity and lineage

Give each trained model a stable identity linked to an actual loadable checkpoint. Use the active project's naming convention and retain historical aliases in the index.

Record the model family and architecture, parent checkpoint, training job and completion status, data and configuration digests, code and dependency revisions, checkpoint location and SHA256, and evaluation coverage. Include task mappings or prompt formats, preprocessing, normalization and tokenizer or processor versions needed to reproduce inference.

Name intermediate checkpoints by training run and step. Track collection, evaluation, repairs and visual analysis as work linked to a model. For interpolation or conversion, record the parent models and transformation. For interrupted, failed or unresolved training, keep its actual state and available checkpoints visible.

Maintain a compact index as work completes. When an old experiment label refers to a repair, evaluation or several training attempts, retain it as an alias and identify the actual checkpoint behind each result. Preserve old reports and weights; add the resolved mapping and its sources to the index.

Keep the chosen baseline, retained behavioral alternatives and incomplete candidates identifiable. Record evidence and conditions for each useful capability. During a comparison, verify that the checkpoint, adapter, data and task conditions match the declared model identity. Keep unresolved lineage explicit until the original records settle it.
