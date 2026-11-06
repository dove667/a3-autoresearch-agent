"""Stage-layer removal respects explicit activation and frozen custom snapshots."""

import hashlib
import json
from pathlib import Path

import pytest

from a3_research_agent.host.bootstrap import input_hashes
from a3_research_agent.host.inputs.versions import (
    SHARED_INPUT_SOURCES,
    InputVersions,
    input_sources,
)

ROOT = Path(__file__).resolve().parents[2]


def custom_bundle(directory, phase, legacy=False):
    directory.mkdir()
    for name, source in input_sources(phase).items():
        (directory / name).write_bytes((ROOT / source).read_bytes())
    if legacy:
        (directory / "phase-instructions.md").write_text("Version: 0.8.0\nLEGACY TASK LAYER\n")
    return directory


@pytest.mark.parametrize("legacy", [False, True])
def test_phase_two_custom_optional_layer_and_frozen_resume(tmp_path, legacy):
    directory = custom_bundle(tmp_path / "custom", "phase-two", legacy)
    run = tmp_path / "run"
    versions = InputVersions(ROOT, run, phase="phase-two", custom_dir=directory)
    proposal = versions.prepare()
    manifest = versions.activate(proposal, "start")
    assert ("phase-instructions.md" in manifest["files"]) is legacy
    assert ("LEGACY TASK LAYER" in versions.model_instructions()[1]) is legacy
    archive = run / "input-revisions" / manifest["revision"]
    original = {p.name: p.read_bytes() for p in archive.iterdir()}
    (directory / "goal.md").write_text("changed source no longer authoritative")
    restored = InputVersions(ROOT, run)
    assert restored.prepare()["contents"] == proposal["contents"]
    restored.activate(restored.prepare(), "resume")
    assert {p.name: p.read_bytes() for p in archive.iterdir()} == original
    assert ("LEGACY TASK LAYER" in restored.model_instructions()[1]) is legacy


def test_phase_one_still_requires_stage_layer_and_legacy_layer_requires_version(tmp_path):
    one = custom_bundle(tmp_path / "one", "phase-one")
    (one / "phase-instructions.md").unlink()
    with pytest.raises(ValueError, match="phase-instructions.md"):
        InputVersions(ROOT, None, custom_dir=one).prepare()
    two = custom_bundle(tmp_path / "two", "phase-two", legacy=True)
    (two / "phase-instructions.md").write_text("unversioned")
    with pytest.raises(ValueError, match="Missing semantic version"):
        InputVersions(ROOT, None, phase="phase-two", custom_dir=two).prepare()


def test_explicit_preset_resume_retires_layer_without_rewriting_archive(tmp_path):
    root = tmp_path / "repo"
    for source in input_sources("phase-two").values():
        path = root / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((ROOT / source).read_bytes())
    legacy_source = "agent-inputs/phase-two/instructions.md"
    (root / legacy_source).write_text("Version: 0.8.0\nOLD PRESET LAYER\n")
    run = tmp_path / "run"
    (run / "private-inputs").mkdir(parents=True)
    old = InputVersions(root, run, phase="phase-two")
    old.sources["phase-instructions.md"] = legacy_source
    old_protocol = "agent-inputs/phase-two/protocol.md"
    (root / old_protocol).write_bytes(
        (Path(__file__).parent / "fixtures/relocated_phase_two_protocol_0_3_0.md").read_bytes()
    )
    old.sources["protocol.md"] = old_protocol
    first = old.activate(old.prepare(), "start")
    archive = run / "input-revisions" / first["revision"]
    original = {p.name: p.read_bytes() for p in archive.iterdir()}
    checkpoint = run / "checkpoint.json"
    checkpoint.write_text('[{"role":"user","content":"old history"}]')
    (root / legacy_source).unlink()
    (root / old_protocol).unlink()
    restored = InputVersions(root, run)
    proposal = restored.prepare()
    # Merely preparing current templates does not mutate an old run.
    assert (run / "inputs/phase-instructions.md").is_file()
    assert (run / "private-inputs/phase-instructions.md").is_file()
    second = restored.activate(proposal, "resume")
    assert "phase-instructions.md" not in second["files"]
    assert second["files"]["protocol.md"]["source"] == "agent-inputs/shared/protocol.md"
    assert first["files"]["protocol.md"]["source"] == old_protocol
    assert (
        json.loads((archive / "manifest.json").read_text())["files"]["protocol.md"]
        == first["files"]["protocol.md"]
    )
    assert "OLD PRESET LAYER" not in restored.model_instructions()[1]
    for mount in ("inputs", "private-inputs"):
        assert not (run / mount / "phase-instructions.md").exists()
    assert (
        run / "input-revisions/legacy/phase-instructions.md"
    ).read_text() == "Version: 0.8.0\nOLD PRESET LAYER\n"
    assert {p.name: p.read_bytes() for p in archive.iterdir()} == original
    assert json.loads(checkpoint.read_text())[0]["content"] == "old history"
    assert "phase-instructions.md" not in input_hashes(run / "inputs")


def test_protocol_reference_revision_preserves_scientific_body():
    old = (Path(__file__).parent / "fixtures/relocated_phase_two_protocol_0_3_0.md").read_text()
    new = (ROOT / "agent-inputs/shared/protocol.md").read_text()
    expected = (
        old.replace("Version: 0.3.0", "Version: 0.3.3")
        .replace(
            "Agent 的具体行动、反馈核查与作业处理见 phase-instructions.md。",
            "长期行为原则见 SOUL.md，实际接口、反馈投递与作业处理见 execution.md。",
        )
        .replace(
            "具体检索与安排动作见 phase-instructions.md。",
            "检索与安排遵循 SOUL.md，恢复及作业状态按 execution.md 核实。",
        )
        .replace(
            "## 1. Policy 完整网格",
            "互相独立且输入已就绪的远程作业，先在权限与资源额度内并行提交，再统一查询与收集结果；"
            "有数据依赖的作业按依赖推进。采集和评估中的独立 episode 统一使用 "
            "`/run/a3/episode_executor.py`，由 Agent 固定科学参数、seed、协议与输出；"
            "不另写固定两个并发的循环，不重复已完成的科学 episode 测吞吐。\n\n"
            "## 1. Policy 完整网格",
        )
        .replace(
            "测试可串行完成，沿用单候选试运行和主身份交接。",
            "完整 Harness 沿唯一线性候选试运行和主身份交接：前任等待自测，候选交权前不独立研究。"
            "当前 Agent 自行完成局部测试；条件独立且资源允许的远程作业先并行提交。",
        )
    )
    # Operational batching changes neither scientific comparisons nor scoring.
    batching = next(line for line in new.splitlines() if line.startswith("一次科学实验可包含"))
    assert new.replace(batching + "\n\n", "") == expected
    assert hashlib.sha256(new.encode()).digest() != hashlib.sha256(old.encode()).digest()


def test_shared_storage_does_not_make_protocol_a_shared_input():
    assert "protocol.md" not in SHARED_INPUT_SOURCES
    assert "protocol.md" not in input_sources("phase-one")
    assert input_sources("phase-two")["protocol.md"] == "agent-inputs/shared/protocol.md"
    assert sorted(p.name for p in (ROOT / "agent-inputs/phase-two").iterdir()) == [
        "acceptance.md",
        "goal.md",
    ]


def test_continuous_acceptance_preserves_scientific_conditions():
    old = (Path(__file__).parent / "fixtures/relocated_phase_two_acceptance_0_3_0.md").read_text()
    new = (ROOT / "agent-inputs/phase-two/acceptance.md").read_text()
    # Behavioral retention expands Policy evidence; benchmark conditions and
    # Harness, attribution, resource and provenance requirements remain intact.
    old_a = old.split("## A.", 1)[1].split("## B.", 1)[0]
    new_a = new.split("## A.", 1)[1].split("## B.", 1)[0]
    assert new_a.splitlines()[-2] == old_a.splitlines()[-2]
    assert "不要求先有总体分数上涨" in new_a
    assert "真实视频与同步轨迹中的具体能力变化" in new_a
    assert "未确认的过程改善保留待验证状态" in new_a
    expected_remaining = old.split("## B.", 1)[1].split("## D.", 1)[0].replace(
        "局部 worker 只负责自己的测试清单。",
        "当前 Agent 自行完成局部检查；唯一正式候选仅自测，前任等待结果，"
        "采用后同一候选接任继续研究。禁止普通 worker 或多 Agent 分叉。",
    )
    assert new.split("## B.", 1)[1].split("## D.", 1)[0] == expected_remaining
    old_delivery = old.split("## D. 终态交付与后续可用\n\n", 1)[1]
    new_delivery = new.split("## D. 阶段交付与后续可用\n\n", 1)[1]
    assert new_delivery.startswith(
        old_delivery.replace("交付最终 Policy、处理器", "按报告快照交付选定的最终 Policy、处理器")
    )
    assert "全部通过也不自动结束持续研究" in new
    assert "顶层仍写 status=continue" in new_delivery
