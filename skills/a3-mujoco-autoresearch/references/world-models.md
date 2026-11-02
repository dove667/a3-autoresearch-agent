# World models and planning

Checked 2026-11-02. This is a conditional research branch, not a prerequisite before imitation learning and not a claim that A3 exposes the required reward/transition interfaces.

## Choose the actual question

A world model is useful when predicting consequences or optimizing beyond demonstrated actions addresses an observed bottleneck. Distinguish an action-conditioned dynamics model from an unconditioned video predictor. Good prediction loss does not establish controllability or correct contact planning.

| Family | Training signal | Control mechanism | Necessary local evidence |
|---|---|---|---|
| TD-MPC2 | Transitions, reward and value/dynamics learning | Receding-horizon latent trajectory optimization | Correct reward/terminal contract, interaction/data coverage, planning latency |
| DreamerV3 | Predictive latent model, reward/continuation; actor–critic in imagined rollouts | Learned recurrent actor at execution | Recurrent reset, replay and real-rollout performance |
| DINO-WM | Offline action-conditioned prediction of pretrained visual features | Optimize an action sequence toward goal features | Reachable goal images, action coverage, task-relevant feature distance |

Sources: [TD-MPC2](https://arxiv.org/abs/2310.16828), [code](https://github.com/nicklashansen/tdmpc2); [DreamerV3](https://arxiv.org/abs/2301.04104), [code](https://github.com/danijar/dreamerv3); [DINO-WM](https://arxiv.org/abs/2411.04983), [code](https://github.com/gaoyuezhou/dino_wm).

Dreamer is **not** online MPC. TD-MPC2's state/pixel variants and online/offline modes have different assumptions. DINO-WM's task evidence is not proof that a visual distance captures A3 slot occupancy, release or stability.

## Interface gate

Record actions, time spacing, observation validity, reward, termination and time-limit truncation. A successful-only demonstration dataset may omit failed contacts/recovery and be inadequate for dynamics outside expert support. Reward shaping, new task definitions or privileged state can change the scientific comparison; obtain the task scope first and label diagnostic tracks separately from fair visual-policy results.

An optional state-input diagnostic can isolate control from representation, but privileged object state cannot be quietly added to the official learned-policy benchmark. Use a genuinely existing short task only if permitted; do not invent “reach mode” as if the project implements it.

## TD-MPC2 experiment branch

Start from the supported small configuration after adapter tests, not a large multitask offline setup. Verify episodic support settings (`episodic=true` where appropriate in the selected version), reward and bootstrap-at-terminal behavior. Compare predicted and actually executed transitions, including contact discontinuities. Track real environment steps, demo prefill and GPU time separately.

Useful hypotheses: planning horizon too long for accurate dynamics; action sampling too broad/out-of-support; sparse reward insufficiently learned; solver can't meet the control deadline. Change horizon or trajectory count independently, measuring latency and real success. Model exploitation can make predicted reward improve while real control deteriorates; retain the mismatch as diagnostic evidence. More planning compute can worsen decisions when the model is wrong.

## DreamerV3 experiment branch

Use matching reward/terminal semantics and a supported small configuration, fixed resolution and recurrent-state reset. Distinguish environment transitions from imagined updates. Diagnose replay composition, reconstruction/latent prediction, reward/continuation estimates, actor entropy, value scale and actual simulator success. Do not retune every loss coefficient before checking terminal bugs and observation timing.

Its actor amortizes execution decisions, but learning/compilation/replay costs can dominate. Actual local throughput and VRAM are unknown until measured. Changing model size or observation resolution is a versioned intervention requiring compatible checkpoint handling.

## Visual-feature planning branch

A bounded DINO-WM-inspired test could freeze an approved encoder, train an action-conditioned predictor on A3 transitions, and assess whether candidate-action rankings correlate with actual task progress. Use camera-stable, reachable goal conditions. If visually similar scenes conceal different contacts, pose ambiguity or progress, diagnose state/feature insufficiency before scaling the predictor.

Evaluate real closed-loop outcomes and planner deadlines. Low feature MSE or attractive predicted frames alone is not evidence. Keep A3's installed MuJoCo stack intact; upstream old-environment install instructions are not a reason to replace it. [DINO-world](https://arxiv.org/abs/2507.19468) is another video-feature research direction, but action conditioning and local planning validation remain necessary.

## Branch stop or redirect signals

- Prediction ranking fails before planning: improve representation/transition coverage or defer.
- Reward rises while official count/success falls: inspect reward mismatch; don't alter the scorer.
- Plans saturate action limits or exploit unseen states: inspect model support and uncertainty; more horizon isn't the default fix.
- State diagnostic works but pixels fail: isolate visual representation/partial observability.
- Good open-loop predictions but poor control: inspect replan timing, contact errors and distribution shift.
- Compute/data prerequisites absent: retain a literature-backed proposal, not a fabricated local implementation.

General VLA/video foundation pretraining is not a default A3 autoresearch experiment. Choose task-level adaptation or a compact model when it directly tests the approved question.
