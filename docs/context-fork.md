# 上下文压缩与记忆（fork / Recall / spill）

本页描述 `runtime/harness_recovery/` 里新增的三段机制。它们是候选 Harness 的运行时能力，不改 Host 权限、不改 A3 物理与判定。

## 三层降级

| 层 | 实现 | 触发 | 是否调模型 |
| --- | --- | --- | --- |
| 1 spill | `spill.py` | 单个工具结果超过 `AUTORUN_SPILL_BYTES`（默认 50000 字节） | 否 |
| 2 fork 摘要 | `compaction.py` | 请求估算超过预算（默认 `WINDOW - RESERVE` = 48000 字节，含运营视图） | 是，异步 |
| 3 fallback checkpoint | `compaction.py` | 摘要为空、报错、或分块超预算 | 否 |

spill 把超限结果写入 `.autorun-spill/<tool>-<sha16>.txt`，模型只看到头 2000 字符、字节数、路径和 `head_complete: false`，需要更多内容时用 `read` 按 offset/limit 取页。`read` 的页收据仍记录原始页，不是 spill 引用。

## 异步 fork 时序

1. 决策前调用 `compactor.before_request(messages, extra_bytes=...)`。
2. 有已完成的压缩结果 → 就地替换历史：`checkpoint 消息 + 最近 5 轮原始消息 + 该结果之后新增的消息`。替换前校验 `function_call` 与 `function_call_output` 配对，不配对则放弃替换并标记 `apply_skipped`（保留未压缩历史与磁盘证据）。
3. 没有结果且超预算 → 快照历史、启动 daemon 线程，**当前这一步继续使用完整历史**。
4. 线程结束后写 `compaction-result.json`（原子写）：
   - 状态 `ready`：结构化摘要；`fallback`：降级检查点。
   - 附带 `prefix_length`、`tail`、`sections`、`strategy`、`calls`、`error`。
5. 下一步决策前应用；应用后若体积未下降 10%，不立即再 fork，等历史真实增长后才重试，避免空转烧钱。

退出前 `compactor.wait()` 并尝试应用一次，保证跨进程也能接上。

## KV-cache 复用

摘要请求复用主请求的 `model`、`instructions`、`tools` 与**完整消息前缀**，只在尾部追加一条压缩指令的 user 消息：

```
input = messages + [{"role": "user", "content": STRUCTURED_INSTRUCTION}]
```

这样 provider 的 prefix cache 能命中同一前缀。未压缩部分过大时（超过 `SUMMARY_INPUT_BYTES` = 24000 字节）退回 `context_manager.chunks_of` 分块，最多 `MAX_SUMMARY_CALLS`（8）次，仍复用同一 model/tools。

## 结构化 checkpoint

摘要必须输出固定八节 Markdown：Primary request and intent / Key technical concepts / Files and code / Errors and fixes / Pending tasks / Current work / Next step / Key context。缺节时追加重写提示并重试一次；仍缺则记录 `sections` 但保留摘要（不静默丢弃）。

外层用 `<compacted-checkpoint>` 包裹，前置一段说明：把它当既定背景、需要细节用 Recall、不要靠推测补全。

## 记忆与 Recall

| 文件 | 作用 |
| --- | --- |
| `history.jsonl` | 每条模型可见消息的完整原文，append-only，压缩不删 |
| `memory.jsonl` | Agent 主动写的经验条目（id/ts/kind/text/sha256/refs），append-only |

工具：

- `recall(keywords, n, m, window=100, limit=30)`：在「n 轮前到 m 轮前」的原文中按关键字返回约 100 字片段，带 `[12~10轮前]` 定位标签。轮次以 user 消息切分。
- `memory(action=write|read|list, content, key, kind)`：写入或读取长期记忆条目。

## 摘要失败时的降级提示

`fallback` 检查点由四段组成，顺序固定：

1. `FALLBACK_PREFIX`：说明较早对话没有写入摘要；
2. `FALLBACK_RULES`：用 Recall 检索原文的调用示例与轮次含义、用 memory 读条目、以 Recall 结果为准、不确定就检索；
3. `memory.jsonl` 条目索引（截断到 2000 字符）；
4. 最近 5 轮短期记忆原文。

即：固定前缀 + 说明 + Recall 指引 + 最近 5 轮原文，绝不静默留下空摘要。

## 相关环境变量

- `AUTORUN_SPILL_BYTES`：spill 阈值，`<=0` 关闭。
- `A3_CONTEXT_*` / `CONTEXT_WINDOW_ESTIMATE` / `CONTEXT_RESERVE_ESTIMATE` / `CONTEXT_SUMMARY_INPUT_ESTIMATE`：沿用 `context_manager` 的预算。

## 平台修正

`context_manager.atomic()` 原先用 `os.open(目录, O_RDONLY)` + `os.fsync` 保证 rename 持久性；Windows 无法打开目录描述符，会让所有 checkpoint 写入抛 `PermissionError`。现在该 fsync 在不可用时跳过，文件内容仍先 fsync 再 `os.replace`。

## 验收

`tests/runtime/test_harness_compaction.py` 覆盖 14 个行为门：spill 阈值/落盘/环境变量、八节结构、不阻塞的 fork 与应用、KV 前缀复用、幂等应用、不配对保守跳过、失败降级内容、Recall 轮次检索、memory 读写。可用 `python tests/runtime/test_harness_compaction.py` 直接运行（无需 pytest）。
