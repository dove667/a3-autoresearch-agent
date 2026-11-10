"""Build a compact behavioral diagnosis handoff from completed episode artifacts."""

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import PurePosixPath

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from a3_research_agent.host.models.vision import VisionFrame

REPORT_BYTES = 256 * 1024
TRACE_BYTES = 8 * 1024 * 1024


class EvaluationComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")
    baseline_job_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    baseline_manifest: str = Field(max_length=1024)
    candidate_manifest: str = Field(max_length=1024)

    @field_validator("baseline_manifest", "candidate_manifest")
    @classmethod
    def relative_manifest(cls, path):
        if not canonical_path(path) or PurePosixPath(path).suffix != ".json":
            raise ValueError("Select a relative JSON manifest in job outputs")
        return path


def canonical_path(path):
    return (
        isinstance(path, str)
        and "\\" not in path
        and all(
            piece not in {"", ".", ".."} and not any(ord(char) < 32 for char in piece)
            for piece in path.split("/")
        )
    )


def read_reports(read, job, manifest_path):
    manifest = json.loads(read(job, manifest_path, REPORT_BYTES))
    episodes = manifest["episodes"]
    if not 1 <= len(episodes) <= 120:
        raise ValueError("Expected 1 to 120 manifest episodes")
    directory = PurePosixPath(manifest_path).parent
    paths = [str(directory / PurePosixPath(item["report"]).name) for item in episodes]
    if len(set(paths)) != len(paths) or not all(
        canonical_path(path) and path.endswith(".json") and path != manifest_path for path in paths
    ):
        raise ValueError("Expected distinct sibling episode reports")

    def load(path):
        try:
            report = json.loads(read(job, path, REPORT_BYTES))
            native = report["native_score"]
            if (
                report["status"] != "completed"
                or type(report["seed"]) is not int
                or type(native["score"]) is not int
                or type(native["max_score"]) is not int
                or not 0 <= native["score"] <= native["max_score"]
                or type(native["success"]) is not bool
            ):
                raise ValueError("Expected a completed native episode report")
            return path, report
        except (HTTPException, KeyError, TypeError, ValueError):
            return path, None

    with ThreadPoolExecutor(max_workers=8) as executor:
        loaded = list(executor.map(load, paths))
    valid, missing = {}, []
    for path, report in loaded:
        if report is None:
            missing.append(path)
            continue
        key = (report["seed"], report["task"], report["profile"])
        if key in valid:
            raise ValueError("Ambiguous repeated episode condition in manifest")
        valid[key] = (path, report)
    return valid, missing


def comparable(before, after):
    for key in ("max_steps", "source_column", "prompt_style", "first_grasp", "success_hold_steps"):
        if before.get(key) != after.get(key):
            return False
    if before["native_score"]["max_score"] != after["native_score"]["max_score"]:
        return False
    # Some runner reports include final outcome counts inside randomization.
    final_fields = {"cookies_in_source", "cookies_in_target", "target_column_counts"}

    def initial(report):
        return {
            key: value
            for key, value in report["native_score"].get("randomization", {}).items()
            if key not in final_fields
        }

    return initial(before) == initial(after)


def trace_evidence(read, job, report_path, label, seed):
    directory = report_path.removesuffix(".json") + "-trace"
    source = {"job_id": job, "area": "outputs", "path": directory + "/trace.jsonl"}
    by_step = {}
    for line in read(job, source["path"], TRACE_BYTES).splitlines():
        row = json.loads(line)
        frame = row.get("frames", {}).get("observation.images.front")
        if not isinstance(frame, dict) or not canonical_path(frame.get("path")):
            continue
        reference = VisionFrame(
            job_id=job,
            area="outputs",
            path=directory + "/" + frame["path"],
            episode_id=f"{label}-{seed}",
            step=row["step"],
            sim_time_s=row["sim_time"],
            camera="front",
        ).model_dump()
        by_step[reference["step"]] = (reference, row)
    return source, by_step


def compact_action(action):
    if not isinstance(action, list) or not 1 <= len(action) <= 32:
        return None
    if not all(type(value) in (float, int) and -1e8 <= value <= 1e8 for value in action):
        return None
    return [round(value, 6) for value in action]


def representative_evidence(read, jobs, reports, seed):
    sources, traces = {}, {}
    missing = []
    for job, report, label in zip(jobs, reports, ("baseline", "candidate"), strict=True):
        try:
            sources[label], traces[label] = trace_evidence(read, job, report, label, seed)
        except (HTTPException, KeyError, TypeError, ValueError):
            missing.append(label)
    if missing:
        return {"trace_sources": sources, "missing_trace_evidence": missing}
    common = sorted(traces["baseline"].keys() & traces["candidate"].keys())
    if not common:
        return {"trace_sources": sources, "missing_trace_evidence": ["no_shared_frame_steps"]}
    steps = sorted({common[(len(common) - 1) * fraction // 10] for fraction in (1, 4, 8)})
    frames, actions = [], {}
    for label, trace in traces.items():
        actions[label] = []
        for step in steps:
            reference, row = trace[step]
            frames.append(reference)
            actions[label].append(
                {"step": step, "policy_action": compact_action(row.get("policy_action"))}
            )
    return {
        "trace_sources": sources,
        "action_samples": actions,
        "vision_request": {
            "question": (
                "Compare the baseline and candidate in this same-seed rollout. Identify visible "
                "differences in approach, grasp, transport, release or recovery. Cite steps, "
                "distinguish observations from hypotheses, and identify intervals to inspect next."
            ),
            "frames": frames,
        },
    }


def evaluation_diagnostics(read, candidate_job, comparison):
    baseline_job = comparison.baseline_job_id
    baseline, missing_baseline = read_reports(read, baseline_job, comparison.baseline_manifest)
    candidate, missing_candidate = read_reports(read, candidate_job, comparison.candidate_manifest)
    pairs, mismatched = [], []
    for key in sorted(baseline.keys() & candidate.keys()):
        before, after = baseline[key][1], candidate[key][1]
        seed, task, profile = key
        if not comparable(before, after):
            mismatched.append({"seed": seed, "task": task, "profile": profile})
            continue
        b, a = before["native_score"], after["native_score"]
        pairs.append(
            {
                "seed": seed,
                "task": task,
                "profile": profile,
                "max_score": b["max_score"],
                "baseline": b["score"],
                "candidate": a["score"],
                "delta": a["score"] - b["score"],
                "baseline_success": b["success"],
                "candidate_success": a["success"],
            }
        )

    failures = sorted(
        (pair for pair in pairs if not pair["candidate_success"]),
        key=lambda pair: (pair["delta"], pair["seed"]),
    )
    chosen = failures[:1]
    if len(failures) > 1:
        chosen.append(failures[-1] if failures[-1]["delta"] > 0 else failures[1])

    def build_case(pair):
        key = (pair["seed"], pair["task"], pair["profile"])
        reports = (baseline[key][0], candidate[key][0])
        return pair | representative_evidence(read, (baseline_job, candidate_job), reports, key[0])

    with ThreadPoolExecutor(max_workers=2) as executor:
        cases = list(executor.map(build_case, chosen))
    return {
        "status": "diagnosis_pending"
        if cases
        else "comparison_available"
        if pairs
        else "evidence_unavailable",
        "baseline_job_id": baseline_job,
        "candidate_job_id": candidate_job,
        "pairs": pairs,
        "representative_cases": cases,
        "missing_reports": {"baseline": missing_baseline, "candidate": missing_candidate},
        "unpaired_conditions": sorted(baseline.keys() ^ candidate.keys()),
        "mismatched_conditions": mismatched,
        "next_step": (
            "Use each vision_request with POST /vision/inspect, then align the observations with "
            "action samples and trace_sources. Locate the earliest supported divergence before "
            "deciding the next experiment. Record missing evidence when a cause cannot be localized."
        ),
    }
