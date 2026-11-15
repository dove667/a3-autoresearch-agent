---
name: a3-frame-cache
description: Use when repeated video decoding limits A3 training, several trainers share decoded frames, cache storage grows or training stops during cache publication.
metadata:
  version: 0.1.0
---

# Cache demonstration frames

Reuse decoded pixels while preserving the declared samples, time alignment, augmentation and task conditions. First measure decode time, data wait, update time and the working set on the actual workload.

## Define a reusable entry

Key entries by source-video digest, exact frame or timestamp selection, decoder version, pixel format and output dimensions. Validate decoded shape and alignment against the source. Apply stochastic augmentation after retrieving the unaugmented frame.

A full-video cache fits workloads whose decoded working set fits the available storage. A bounded block cache can reduce peak storage; decoding from a preceding keyframe must reproduce the selected frames from a sequential reference decode. Verify boundary frames and timestamps on real multi-camera samples.

## Publish and evict safely

Use a dedicated cache namespace with an explicit capacity. Write temporary content, validate it, and publish the data and commit metadata under the cache's own locking protocol. Readers accept committed entries and hold the required protection until the frame is copied. Eviction must honor active readers and writers. Different cache implementations need distinct namespaces and locking protocols.

After an interrupted writer, identify active trainers before repair. Under an exclusive maintenance lock, quarantine partial or orphan entries and preserve their metadata. Rebuild missing entries from verified source data; leave committed valid pairs intact. Include quarantine in disk accounting. If a suitable implementation is absent, implement and test these invariants before pointing training at it.

## Compare the actual pipeline

Use matched data, sampler, augmentation, batch size, model, training steps and resource conditions. Record cold and warm cache behavior, actual decodes and writes, disk use and valid updates per elapsed time. Preserve compressed source videos and lightweight indexes.

Keep running training intact. Apply a cache change in a new authorized job or resume from a complete model, optimizer, scheduler and RNG checkpoint. Recheck sample identity after the change.
