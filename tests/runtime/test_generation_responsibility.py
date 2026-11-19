"""Published inputs distinguish research work from Harness maintenance."""

from pathlib import Path

from a3_research_agent.host.inputs.versions import InputVersions

ROOT = Path(__file__).resolve().parents[2]


def test_published_research_contract(tmp_path):
    versions = InputVersions(ROOT, tmp_path / "run")
    versions.activate(versions.prepare(), "start")
    instructions = versions.model_instructions()[1]
    for requirement in (
        "在路线内根据结果提出小假设，直接用工具验证",
        "一条路线回答一个能影响策略选择的大问题",
        "整批尝试上限",
        "人类和 Codex 在运行暂停期间维护",
        "夜间只使用部署方提供的 Harness",
    ):
        assert requirement in instructions
    assert "每代在交权前" not in instructions


def test_research_web_is_an_explicit_optional_service():
    execution = (ROOT / "prompt/execution.md").read_text()
    assert "网页服务默认关闭" in execution and "web_status" in execution
    assert "缺少认证或服务时报告维护者" in execution
    for tool in ("web_status", "web_search", "web_fetch"):
        assert tool in execution
    assert "/web/search" not in execution
