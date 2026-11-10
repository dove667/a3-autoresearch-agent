"""Published inputs keep research and daytime maintenance responsibilities separate."""

from pathlib import Path

import pytest

from a3_research_agent.host.inputs.versions import InputVersions

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("phase", ["phase-one", "phase-two"])
def test_published_research_contract(tmp_path, phase):
    versions = InputVersions(ROOT, tmp_path / "run", phase=phase)
    versions.activate(versions.prepare(), "start")
    instructions = versions.model_instructions()[1]
    for requirement in (
        "分析结果 → 提出假设 → 规划路线 → 执行 → 记录结果 → 反思分析结果",
        "整批尝试上限",
        "人类和 Codex 在运行暂停期间维护",
        "夜间只使用部署方提供的 Harness",
    ):
        assert requirement in instructions
    assert "每代在交权前" not in instructions


def test_research_web_is_optional_in_policy_research():
    prompt = (ROOT / "agent-inputs/phase-two/goal.md").read_text()
    execution = (ROOT / "agent-inputs/shared/execution.md").read_text()
    assert "Host 默认关闭网页网关，部署方启用并接入工具后使用" in execution
    assert "缺失注册或后端故障交给维护者" in execution
    for endpoint in ("/web/status", "/web/search", "/web/fetch"):
        assert endpoint in execution
        assert endpoint not in prompt
        for phase in ("phase-one", "phase-two"):
            assert endpoint not in (ROOT / f"agent-inputs/{phase}/acceptance.md").read_text()
