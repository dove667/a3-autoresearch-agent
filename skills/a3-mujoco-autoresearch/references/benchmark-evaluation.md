# A3 full benchmark evaluation

The long-term research goal in SOUL is to improve A3 2×5 and 2×10 full-evaluation accuracy and success rate. Use the following comparison conditions when measuring progress, and save the resolved settings with each experiment.

## Coverage and conditions

| Task | Difficulties | Episodes per difficulty | Final-count capacity | Maximum control steps | Instruction style |
| --- | --- | --- | --- | --- | --- |
| 2×5 | basic, medium, advanced | 20 | 10 | 500 | `training`, short instruction |
| 2×10 | basic, medium, advanced | 20 | 20 | 3200 | `short`, twenty-cookie instruction |

The complete grid has six cells and 120 episodes. Both tasks use 20 Hz control and source column 1, with the task's actual randomization settings. Save the resolved instruction text, first-grasp/task settings, A3 revision and relevant configuration.

Use one fixed policy snapshot across the grid: weights, processor and statistics, adapter/model code, inference settings, and any task-dependent routing. Fix and record routing before evaluating; preserve it with the snapshot.

Baseline and candidate use matching scene seeds and external conditions, with inference seeds matching the corresponding scene seeds. Save the actual seed list. Historical runs with different step limits, geometry, prompts or other conditions retain their original records and comparison scope.

## Daily iteration and final comparisons

Use small, explicitly selected development subsets to diagnose failures and compare intermediate candidates. Run the complete grid for promising candidates, after fixing their snapshot. Keep training/data-selection scenarios, exploratory evaluation scenarios and reserved final-comparison scenarios separate. A scenario used repeatedly to select or tune candidates becomes development evidence.

Reuse valid completed episodes from the same snapshot and conditions. Reconcile pending or uncertain work before resuming. Crashes, missing reports and unknown results remain execution issues; preserve raw reports and repair or recover the affected work. Partial coverage is reported with the actual cells and episode counts.

## Report progress

For each cell, report the episode count, final `cookies_in_target`, its mean divided by task capacity (10 or 20), and the original Boolean `success` count/rate. Preserve the native success conditions, including placement, release, stability and safety. Final count and native success remain separately visible.

An overall normalized final-count summary may average the six cell ratios equally. Report paired scenario differences and uncertainty alongside per-cell results; use them to choose the next experiment.

Link the snapshot, dataset, resolved evaluation settings, job IDs and raw episode records. Keep scientific task failures separate from execution failures, and describe behavioral improvements with synchronized visual/action evidence when useful.
