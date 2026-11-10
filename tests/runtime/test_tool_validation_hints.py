import ast
import pathlib

import pytest

from a3_research_agent.harness import planning, skill_registry

ROOT = pathlib.Path(__file__).resolve().parents[2] / "src/a3_research_agent/harness"


def functions():
    tree = ast.parse((ROOT / "agent.py").read_text())
    nodes = [
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef)
        and n.name in {"dispatch_tool_call", "spec", "strings"}
        or isinstance(n, ast.Assign)
        and any(
            isinstance(x, ast.Name) and x.id in {"SAFE_VALIDATION_ERRORS", "S", "tools"}
            for x in n.targets
        )
    ]
    ns = {
        "json": __import__("json"),
        "web_tools": None,
        "skill_tools": None,
        "skill_registry": skill_registry,
        "planning": planning,
    }
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "trusted-agent-functions", "exec"), ns)  # noqa: S102
    return ns


def test_known_validation_returns_safe_actionable_hint():
    ns = functions()

    def tool(*a):
        raise ValueError("Invalid bounded operational item")

    ns["tool"] = tool
    result = ns["dispatch_tool_call"]({"name": "progress", "arguments": "{}"})
    assert result["error"] == "validation_failed"
    assert result["code"] == "progress_bounds"
    assert "400" in result["hint"]


@pytest.mark.parametrize(
    "error",
    [
        ValueError("SECRET /private/path token=xyz"),
        RuntimeError("SECRET"),
        PermissionError("SECRET"),
    ],
)
def test_unknown_and_permission_errors_never_echo_exception_text(error):
    ns = functions()

    def tool(*a):
        raise error

    ns["tool"] = tool
    result = ns["dispatch_tool_call"]({"name": "progress", "arguments": "{}"})
    assert "SECRET" not in str(result)
    assert result["error"] != "validation_failed"


def test_schema_exposes_actual_bounded_parameters():
    tools = {x["name"]: x["parameters"]["properties"] for x in functions()["tools"]}
    assert tools["progress"]["note"]["maxLength"] == 400
    assert tools["progress"]["evidence"]["maxItems"] == 8
    assert "delegate" not in tools
    assert tools["wait"]["seconds"]["maximum"] == 600


def test_input_header_names_the_canonical_manifest_path(tmp_path):
    from test_input_versions import sources

    from a3_research_agent.host.inputs.versions import InputVersions

    v = InputVersions(sources(tmp_path), tmp_path / "run")
    v.activate(v.prepare(), "start")
    assert "/run/input/input-manifest.json" in v.model_instructions()[1]


def test_real_progress_validation_does_not_mutate_state(tmp_path, monkeypatch):
    import importlib.util

    monkeypatch.chdir(tmp_path)
    spec = importlib.util.spec_from_file_location(
        "real_progress_validation", ROOT / "memory/state.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    ns = functions()
    ns["tool"] = lambda name, args: module.progress(args)
    result = ns["dispatch_tool_call"](
        {
            "name": "progress",
            "arguments": __import__("json").dumps(
                {"action": "update", "id": "x", "status": "open", "note": "x" * 401}
            ),
        }
    )
    assert result["code"] == "progress_bounds"
    assert not module.PATH.exists()


def test_whitelist_is_exact_and_scoped_to_the_expected_tool():
    ns = functions()

    def tool(*a):
        raise ValueError("Invalid bounded operational item SECRET")

    ns["tool"] = tool
    assert "hint" not in ns["dispatch_tool_call"]({"name": "progress", "arguments": "{}"})

    def wrong_scope(*a):
        raise ValueError("Invalid bounded operational item")

    ns["tool"] = wrong_scope
    assert "hint" not in ns["dispatch_tool_call"]({"name": "read", "arguments": "{}"})
