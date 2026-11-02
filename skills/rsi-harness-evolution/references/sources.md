# Primary source map

Sources checked on 2026-11-02. This map supports mechanisms and design alternatives, not a claim that their results transfer to A3 or any particular base model. Component cards and proposed experiments are original engineering synthesis; no upstream code is copied.

## Agent interfaces and context

- [SWE-agent paper](https://arxiv.org/abs/2405.15793): Agent–computer interface design for software tasks. Supports testing the interface as a variable, not always preferring bespoke tools.
- [mini-SWE-agent official documentation](https://mini-swe-agent.com/latest/): a deliberately simple tool/control baseline. Its simplicity is useful as a comparison, not a reason to ignore an approved long-context task.
- [Writing tools for agents](https://www.anthropic.com/research/writing-tools-for-agents): task-grounded tool design and evaluation.
- [Effective context engineering](https://www.anthropic.com/research/effective-context-engineering-for-ai-agents): selective context, compaction, notes and context isolation.
- [OpenAI session-memory example](https://developers.openai.com/cookbook/examples/agents_sdk/session_memory): trimming and summarization patterns in a particular SDK. The A3 gateway does not thereby acquire that SDK's APIs.
- [Reflexion](https://arxiv.org/abs/2303.11366): linguistic feedback carried across trials; not proof of code self-modification.
- [MemGPT](https://arxiv.org/abs/2310.08560): explicit management of memory tiers; not unlimited lossless memory.
- [A-MEM](https://arxiv.org/abs/2502.12110), [author code](https://github.com/agiresearch/A-mem): linked evolving notes; an optional research mechanism, not a default database requirement.
- [Agent Skills engineering](https://www.anthropic.com/research/equipping-agents-for-the-real-world-with-agent-skills): progressive disclosure of procedural files and resources.

## Control, collaboration and measurement

- [Building effective agents](https://www.anthropic.com/research/building-effective-agents): composable workflows and agents; use patterns selectively.
- [Multi-agent research system](https://www.anthropic.com/research/multi-agent-research-system): breadth-oriented independent work and its coordination costs. Research results do not prove gains for sequential coding tasks.
- [Demystifying evaluations for agents](https://www.anthropic.com/research/demystifying-evals-for-ai-agents): outcomes, trajectories and complementary graders.
- [Provider prompt-caching documentation](https://platform.claude.com/docs/en/build-with-claude/prompt-caching): a concrete provider's prefix semantics. Do not copy its parameters or billing assumptions to Terra/A3 without support.

## Search and self-improvement

- [HarnessPro v2](https://arxiv.org/abs/2610.26770v2), [authors' project](https://pzs19.github.io/HarnessPro/), [code](https://github.com/pzs19/HarnessPro): recent fixed-weight, trajectory-guided Harness code improvement. Under-review author evidence, not an independent A3 replication.
- [Darwin Gödel Machine](https://arxiv.org/abs/2505.22954), [code](https://github.com/jennyzzt/dgm): self-modifying coding agents and an archive; outer selection machinery remains a distinct design.
- [STOP](https://arxiv.org/abs/2310.02304): an LM-based optimizer improving its scaffold; do not overstate it as unrestricted recursive improvement.
- [ADAS](https://arxiv.org/abs/2408.08435): automated agent-design search. Generating downstream agents is not automatically main-role takeover.
- [GEPA](https://arxiv.org/abs/2507.19457), [official library](https://github.com/gepa-ai/gepa): reflective feedback for textual component optimization. Prompt evolution and full Harness code evolution are distinct.
- [AIDE](https://arxiv.org/abs/2502.13138), [reference implementation](https://github.com/wecoai/aideml): ML task-solution search. Candidate lineage is transferable inspiration; recursive self-Harness modification is not the original result.

For local truth, read the active goal/acceptance/execution/protocol and the loaded source, not this bibliography. Maintain verified citations, counterexamples and version conditions when updating a recipe. New ideas outside this map are encouraged when they address observed problems and can be tested.
