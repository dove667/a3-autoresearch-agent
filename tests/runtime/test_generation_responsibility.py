"""Offline contracts for generation-local accountability in published inputs."""

import re
from pathlib import Path

import pytest

from runtime.local.input_versions import InputVersions

ROOT = Path(__file__).resolve().parents[2]


def assert_generation_contract_version(text):
    match = re.search(r"^Version: (\d+)\.(\d+)\.(\d+)$", text, re.MULTILINE)
    assert match is not None
    assert tuple(map(int, match.groups())) >= (0, 3, 0)


@pytest.mark.parametrize("phase", ["phase-one", "phase-two"])
def test_published_generation_contract(tmp_path, phase):
    versions = InputVersions(ROOT, tmp_path / "run", phase=phase)
    versions.activate(versions.prepare(), "start")
    instructions = versions.model_instructions()[1]
    prompt = (
        ROOT / f"agent-inputs/{phase}/" / ("instructions.md" if phase == "phase-one" else "goal.md")
    ).read_text()
    assert prompt in instructions
    if phase == "phase-two":
        # Canonical requirements are injected acceptance/protocol, not duplicated
        # in the thin stage bridge; execution remains mounted and referenced.
        assert "每代在交权前核查自己负责的" in instructions
        for clause in (
            "假设先于执行 → 实际结果 → 保留/拒绝/证据不足决定",
            "后继继承已确认结论，无需逐条重审前任",
            "接手未完任务只承担接手后的新执行责任",
            "前任历史缺口须标明归属",
            "未完成或未知不得写成已确认",
            "明确矛盾可针对性核查",
        ):
            assert clause in instructions
        assert "execution.md" in prompt
        assert_generation_contract_version(prompt)
        return
    for clause in (
        "每代主 Agent 只对自己负责的实验过程与决定承担验收责任",
        "先提出并记录假设，再执行，最后依据实际结果决定保留、拒绝或证据不足",
        "前任已确认结论作为已知结论继承",
        "不要求读取全部历史消息或逐条重验前任实验",
        "明确矛盾时可针对性核查",
        "未完成或未知不得写成已确认",
        "接手明确未完成事项是新的执行责任，不接管前任过去的合规责任",
        "前任历史缺口仍归前任，不因换代消失",
        "候选在接任前无需完成整个 run",
        "不重新执行或审计前任原实验",
    ):
        assert clause in prompt
    assert "自行完成本轮全部验收" not in prompt
    assert_generation_contract_version(prompt)


@pytest.mark.parametrize("phase", ["phase-one", "phase-two"])
def test_acceptance_is_run_coverage_not_repeated_generation_gate(phase):
    acceptance = (ROOT / f"agent-inputs/{phase}/acceptance.md").read_text()
    assert "各代已确认结论与证据引用" in acceptance
    assert "不是每代重复执行全部验收的门槛" in acceptance
    assert "未完成或未知不得写成已确认" in acceptance
    assert "存在未覆盖项时不得声明整轮全部通过" in acceptance
    assert_generation_contract_version(acceptance)


def test_protocol_and_execution_preserve_local_handoff_boundary():
    protocol = (ROOT / "agent-inputs/shared/protocol.md").read_text()
    assert "不因主身份交接而重复最终 120 回合网格或已完成的三个配对实例" in protocol
    assert "每代核对自己负责的实验" in protocol
    execution = (ROOT / "agent-inputs/shared/execution.md").read_text()
    assert "交权前完成自己负责的实验闭环核查" in execution
    assert "候选在接任前无需完成整个 run" in execution
    assert "不重新执行或审计前任原实验" in execution
    for text in (protocol, execution):
        assert_generation_contract_version(text)


def test_research_web_is_optional_phase_two_function_integration():
    prompt = (ROOT / "agent-inputs/phase-two/goal.md").read_text()
    execution = (ROOT / "agent-inputs/shared/execution.md").read_text()
    for endpoint in ("/web/status", "/web/search", "/web/fetch"):
        assert endpoint in execution
        assert endpoint not in prompt
    assert "不向模型自动注册 function tools" in execution
    assert "不因新任务默认重做通用能力建设" in execution
    assert "未配置" in execution
    assert "execution.md" in prompt
    first_phase = (ROOT / "agent-inputs/phase-one/instructions.md").read_text()
    assert "/web/" not in first_phase
    for phase in ("phase-one", "phase-two"):
        acceptance = (ROOT / f"agent-inputs/{phase}/acceptance.md").read_text()
        assert "/web/" not in acceptance
