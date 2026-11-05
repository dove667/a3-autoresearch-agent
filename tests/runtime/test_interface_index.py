"""References remain complete; the index only selects the needed character pages."""

import hashlib

from test_input_versions import INPUT_SOURCES, sources

from a3_research_agent.host.inputs.versions import InputVersions


def test_execution_sections_match_raw_character_offsets_without_injection(tmp_path):
    root = sources(tmp_path)
    text = "Version: 9.0.0\r\n前言🌍\r\n## Alpha\r\n参数α\r\n### nested\r\nbody\r\n## Beta\r\nend"
    (root / INPUT_SOURCES["execution.md"]).write_bytes(text.encode())
    versions = InputVersions(root, tmp_path / "run")
    manifest = versions.activate(versions.prepare(), "start")
    index = manifest["execution_index"]
    assert index["unit"] == "characters"
    assert index["sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert (tmp_path / "run/inputs/execution.md").read_bytes() == text.encode()
    assert [x["title"] for x in index["sections"]] == ["Alpha", "Beta"]
    for section in index["sections"]:
        assert text[section["offset"] : section["end"]].startswith("## " + section["title"])
    _, prompt = versions.model_instructions()
    assert "参数α" not in prompt
    assert "execution_index" in prompt
    assert "无需" in prompt
    for name in ["SOUL.md", "goal.md", "acceptance.md", "phase-instructions.md"]:
        assert versions.contents[name] in prompt


def test_custom_many_long_headings_produce_bounded_index_not_lost_source():
    import json

    from a3_research_agent.host.inputs.versions import execution_index

    text = "".join("## " + ("长" * 400) + str(i) + "\nbody\n" for i in range(400))
    index = execution_index(text)
    assert index["truncated"] is True
    assert len(json.dumps(index, ensure_ascii=False).encode()) <= 8000
    assert text[index["next_offset"] :].startswith("## ")
    assert index["sha256"] == hashlib.sha256(text.encode()).hexdigest()


def test_real_phase_two_core_is_unchanged_and_execution_stays_reference_only(tmp_path):
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    versions = InputVersions(root, tmp_path / "run", phase="phase-two")
    versions.activate(versions.prepare(), "start")
    _, prompt = versions.model_instructions()
    for name in ["SOUL.md", "goal.md", "acceptance.md", "protocol.md"]:
        assert versions.contents[name] in prompt
    assert versions.contents["execution.md"] not in prompt
    assert (
        versions.manifest["execution_index"]["sha256"]
        == versions.manifest["files"]["execution.md"]["sha256"]
    )
