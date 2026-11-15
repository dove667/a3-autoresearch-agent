"""Read the native workspace and input roots configured by the Host."""

import os
from pathlib import Path


def _root(name):
    value = os.environ.get(name)
    if not value or not Path(value).is_absolute():
        raise RuntimeError("Host must configure absolute native runtime roots")
    return Path(value).resolve()


def workspace_root():
    return _root("A3_WORKSPACE")


def input_root():
    return _root("A3_INPUT_ROOT")


def reference_root():
    return _root("A3_REFERENCE_ROOT")


def memory_root():
    return _root("A3_MEMORY_ROOT")


def skills_root():
    return _root("A3_SKILLS_ROOT")


def skills_history_root():
    value = os.environ.get("A3_SKILLS_HISTORY_ROOT")
    return (
        _root("A3_SKILLS_HISTORY_ROOT")
        if value
        else skills_root().parent / ".runtime/skill-revisions"
    )
