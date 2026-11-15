"""Reference indexes select exact character ranges without injecting the full text."""

import hashlib
import json
from pathlib import Path

from input_source_support import deployment_source

from a3_research_agent.host.inputs.versions import InputVersions, execution_index

ROOT = Path(__file__).resolve().parents[2]


def test_execution_sections_preserve_original_bytes_and_character_offsets(tmp_path):
    root = deployment_source(tmp_path / "source")
    text = "Version: 9.0.0\r\n前言🌍\r\n## Alpha\r\n参数α\r\n### nested\r\nbody\r\n## Beta\r\nend"
    (root / "prompt/execution.md").write_bytes(text.encode())
    inputs = InputVersions(root, tmp_path / "run")
    manifest = inputs.activate(inputs.prepare(), "start")
    index = manifest["execution_index"]
    assert index["unit"] == "characters"
    assert index["sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert (inputs.run / "inputs/execution.md").read_bytes() == text.encode()
    assert [section["title"] for section in index["sections"]] == ["Alpha", "Beta"]
    for section in index["sections"]:
        assert text[section["offset"] : section["end"]].startswith("## " + section["title"])
    prompt = inputs.model_instructions()[1]
    assert "参数α" not in prompt and "execution_index" in prompt


def test_many_long_headings_have_bounded_index_and_lossless_source_identity():
    text = "".join("## " + "长" * 400 + str(index) + "\nbody\n" for index in range(400))
    index = execution_index(text)
    assert index["truncated"] is True
    assert len(json.dumps(index, ensure_ascii=False).encode()) <= 8000
    assert text[index["next_offset"] :].startswith("## ")
    assert index["sha256"] == hashlib.sha256(text.encode()).hexdigest()


def test_real_runtime_reference_is_mounted_without_repeating_its_body(tmp_path):
    inputs = InputVersions(ROOT, tmp_path / "run")
    inputs.activate(inputs.prepare(), "start")
    prompt = inputs.model_instructions()[1]
    assert inputs.contents["execution.md"] not in prompt
    assert (
        inputs.manifest["execution_index"]["sha256"]
        == inputs.manifest["files"]["execution.md"]["sha256"]
    )
