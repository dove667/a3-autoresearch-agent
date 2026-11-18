"""A leaked protocol delimiter must not cost a decision round."""

import json

import pytest

from a3_research_agent.harness import tool_json

PIPE = chr(0xFF5C)


def test_end_tag_inside_arguments_is_removed():
    raw = '{"job_ids":["3c33c45428e34f0ea9e326c2ed6443cf"],"timeout_seconds":1800}</parameter>'
    assert tool_json.loads(raw) == {
        "job_ids": ["3c33c45428e34f0ea9e326c2ed6443cf"],
        "timeout_seconds": 1800,
    }


def test_pipe_wrapped_delimiter_is_removed():
    raw = '{"path":"report.json"' + "<" + PIPE + "DSML" + PIPE + ">}"
    assert tool_json.loads(raw) == {"path": "report.json"}


def test_open_parameter_tag_is_removed():
    raw = '<parameter name="job_id">{"job_id":"' + "a" * 32 + '"}</parameter>'
    assert tool_json.loads(raw) == {"job_id": "a" * 32}


def test_real_syntax_error_still_fails():
    with pytest.raises(ValueError):
        tool_json.loads('{"job_id": }')


def test_non_object_json_is_returned_for_the_caller_to_reject():
    """The tool layer reports invalid_arguments; parsing only reports syntax."""
    assert tool_json.loads("[1, 2, 3]") == [1, 2, 3]


def test_clean_text_is_unchanged():
    raw = json.dumps({"task": "2x10", "max_steps": 3200})
    assert tool_json.loads(raw) == {"task": "2x10", "max_steps": 3200}
