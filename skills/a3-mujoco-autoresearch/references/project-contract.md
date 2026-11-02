# Verified project contract

Inspected 2026-11-02. Upstream A3 source: **6ff458b0b73b78a3ffd794e8b37fdf297a23af48**, [pinned tree](https://github.com/Eter0109/a3_dual_arm_sim/tree/6ff458b0b73b78a3ffd794e8b37fdf297a23af48). RSI source at authorship: 401edc4; phase-two protocol 0.3.0. Neither proves an old run used these versions. At execution record `/opt/a3/.git`, `/opt/a3/source-revision.json`, actual file/dirty hashes, frozen `/run/input` and installed packages.

The supplied course PDF (`midterm(2).pdf`, three pages) motivates ACT, Diffusion/Flow, VLA, World Model and an autonomous Analyze→Hypothesize→Implement→Train→Evaluate→Reflect→Iterate loop. Its expert6000-step example is not the phase-two evaluation budget.

## Environment and policy boundary

[contracts](https://github.com/Eter0109/a3_dual_arm_sim/blob/6ff458b0b73b78a3ffd794e8b37fdf297a23af48/docs/contracts.md), [contract types](https://github.com/Eter0109/a3_dual_arm_sim/blob/6ff458b0b73b78a3ffd794e8b37fdf297a23af48/src/a3_dual_arm_sim/contracts.py), [environment](https://github.com/Eter0109/a3_dual_arm_sim/blob/6ff458b0b73b78a3ffd794e8b37fdf297a23af48/src/a3_dual_arm_sim/sim/env.py):

- `joint_position`: finite 16D **absolute** targets `[L1..L7,L_gripper,R1..R7,R_gripper]`; radians, gripper 0 = closed / 1 = open. Not deltas/torques/velocities. Per-arm order SHOULDER_P, SHOULDER_R, SHOULDER_Y, ELBOW_P, WRIST_R, WRIST_P, WRIST_Y.
- `cartesian_delta`: separate 14D interface, two six-dimensional normalized pose increments plus gripper, converted by IK. Never reinterpret a 14D vector as the original ACT joint vector.
- `observation.state`:16 ordered joint/gripper values; `observation.velocity`:16 rates.
- `observation.eef_pose`:14, left then right world xyz + quaternion(wxyz). `observation.force`:18 heterogeneous wrist force/torque, finger touch and summed finger-actuator signals. Declare which channels are permitted/modelled, not automatically all.
- Images: RGB uint8 HWC front/left_wrist/right_wrist under `observation.images.*`; training examples256×256. Verify actual geometry and processing.
- Cookie control20 Hz, physics1000 Hz. Recorder pairs pre-action observation with `info['applied_action']`; limits/rate limiting can change raw model predictions. Observe raw, processed and applied actions separately.
- Public EEF/force signals must be declared inputs. Cookie/target-slot truth and expert action are privileged and cannot silently enter learned-policy inference.

A learned plugin exposes `action_mode='joint_position'`, `reset(EpisodeContext(...))`, `act(observation,task='')->finite 16D`, optional close/bind_env. Zero-argument `module:factory` owns configuration. Clear all recurrent/chunk state at reset; bind_env is not a license to read privileged truth.

## 8D, 11D, 16D are different contracts

Pinned [ACT](https://github.com/Eter0109/a3_dual_arm_sim/blob/6ff458b0b73b78a3ffd794e8b37fdf297a23af48/src/a3_dual_arm_sim/policies/act.py) and [SmolVLA](https://github.com/Eter0109/a3_dual_arm_sim/blob/6ff458b0b73b78a3ffd794e8b37fdf297a23af48/src/a3_dual_arm_sim/policies/smolvla.py) support `(action_dim,state_dim)`=(8,8),(8,11),(16,16).

An 8D policy controls left7+gripper. Adapter captures rightstate[8:16] from the episode's first observation and holds it; **never pad right targets with zeros**. Merely running in a 16-actuator simulator does not require retraining a dual-arm policy. The 11D state path is left 8 plus left EEF xyz, not 11 action dimensions; it requires matching training features and declared observation scope.

For genuine 8→16 adaptation, inspect the exact checkpoint rather than its name:

- LeRobot 0.5.1 SmolVLA uses `max_state_dim`/`max_action_dim` projections and pads smaller physical features; the official base has maxima 32. Output/loss are sliced to physical dimensions. A3's trainer checks capacity, rewrites feature shapes and reuses weights. Thus compatible 8→16 adaptation need not reinitialize the action projection. It **does** need genuine 16D data, new matching statistics/processors and training; first 8 supervision does not teach A3 right-arm behavior.
- ACT's physical dimensions enter its VAE state/action projections, encoder state projection and final head. Converting8→16 is not only replacing the final head. Shared visual/transformer weights and overlapping blocks could be transferred deliberately, with new blocks initialized and trained. This is a proposed conversion, not an available A3 continuation command.

[SmolVLA pinned implementation](https://github.com/huggingface/lerobot/blob/1396b9fab7aecddd10006c33c47a487ffdcb54b4/src/lerobot/policies/smolvla/modeling_smolvla.py), [ACT pinned implementation](https://github.com/huggingface/lerobot/blob/1396b9fab7aecddd10006c33c47a487ffdcb54b4/src/lerobot/policies/act/modeling_act.py). Exact Eter checkpoint configs were not inspected here; verify local assets before extrapolating from base configuration.

## Data and training surfaces

A3 pins public LeRobot 0.5.1/Python 3.12+; never blindly use newer tutorial imports. Published `Eter0109/a3-front-close-left-100` at revision8fdd8a7caf8275eba8be8a83b53a99176a084062 has 100 episodes / 42681 frames / 20 FPS / 8D state/action/three 256 RGB fields, LeRobot v3.0. These numbers do not describe `a3-cookie-multi-tasks` or prove local availability.

Raw collection defaults 16D. `prepare_left_arm_dataset.py` creates a distinct 8D dataset and statistics; do not convert twice. Retain meta/data/videos, collection_summary.json, attempts.jsonl and a3_episode_metadata.jsonl. Symlinked video data may depend on original directories. Official expert collectors are not arbitrary-policy collectors; generic recorded failures must not receive forged expert-success metadata.

Verified entrypoints: `a3-sim train-act`, `a3-sim train-smolvla`, and `examples/audit_smolvla_data.py` (also useful for ACT). Audit sampling is not proof all frames align. `--dry-run` writes files. ACT defaults and SmolVLA tutorial/config settings differ: always save resolved config. SmolVLA `num_steps` is flow inference iterations, not optimizer updates. `load_vlm_weights=false` can avoid double loading a full checkpoint; it does not mean random VLM initialization.

Checkpoint processors, statistics and model weights travel together. Auto-resolution can select latest step/EMA; pin an explicit resolved directory and hashes for comparison. Offline first-action error on training observations is not independent closed-loop evaluation.

## Evaluation interfaces and scoring

Pinned single-cell benchmark supports `--policy expert|smolvla:<path>|act:<path>|pi05:<path>|module:factory`. The `evaluate_smolvla_grid_cli` **hardcodes SmolVLAPolicyAdapter**; do not invent a generic `--policy` flag. Other families need the common benchmark API or explicit single-cell runs preserving all conditions. A custom factory owns its parameters; built-in model CLI overrides don't configure arbitrary plugins.

Single CLI resets one inference seed each episode; grid uses scenario seed per episode. Ensure paired evaluation implements the active protocol explicitly.

2×5 transfers 10 of 80 cookies with slot/boundary, release/stability/safety requirements;2×10 transfers 20 with column counts10/10 and different slot requirements. Capture actual success_conditions rather than hardcode a simplified “count equals capacity”. Final cookies_in_target and complete success are separate. Never report peak count as final count.

Phase-two0.3.0 fixes source column 1,2×5 training-short instruction / 500 steps and2×10 short20-cookie instruction / 3200 steps,20 Hz. Six cells × 20 scenarios = 120 per fixed snapshot; development subsets permitted. Same snapshot includes processor/code/inference/routing, not just weights. Future run's actual protocol is authoritative. See [evaluation helper](evaluation-helper.md) for raw schemas, pairing and uncertainty.

Pi0.5 caveat: current benchmark code still requires task 2×5 and pi05_aligned prompt, despite README linking a dual-scene checkpoint. `--pi05-scene-mode full` changes robot/cookie/bin/camera arrangements and is a **different protocol**, not evidence of a fair phase-two gain. This is an integration gap, not proof the method family cannot work.

## Runtime facts to recheck

Candidate workspace is writable; `/run/input` and `/opt/a3` read-only. Remote jobs use `/workspace`; use a verified Python module or absolute `/opt/a3/examples/...`, not assumed relative paths. Model/data caches may be read-only; jobs can be offline. Missing weights/tokenizers/backbones are concrete resource gaps, not permission to fetch. Use authorized persistent outputs for large artifacts, not small text APIs. Actual deployment/job limits and GPU allocation govern; example config values and published GPU estimates are not resource reservations.
