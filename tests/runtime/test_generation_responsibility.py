"""Published inputs distinguish research work from Harness maintenance."""

from pathlib import Path

from task_input_support import task_inputs

from a3_research_agent.host.inputs.versions import InputVersions

ROOT = Path(__file__).resolve().parents[2]


def test_published_research_contract(tmp_path):
    versions = InputVersions(ROOT, tmp_path / "run", custom_dir=task_inputs(tmp_path / "task"))
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


def test_research_web_is_an_explicit_optional_service():
    execution = (ROOT / "agent-inputs/shared/execution.md").read_text()
    assert "Host 默认关闭网页网关，部署方启用并接入工具后使用" in execution
    assert "缺失注册或后端故障交给维护者" in execution
    for endpoint in ("/web/status", "/web/search", "/web/fetch"):
        assert endpoint in execution
