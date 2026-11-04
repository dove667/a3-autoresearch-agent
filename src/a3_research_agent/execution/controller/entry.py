"""Construct background commands without relying on inherited PYTHONPATH."""

import sys
from pathlib import Path


def controller_command(*arguments: str) -> list[str]:
    return [sys.executable, str(Path(__file__).with_name("__main__.py")), *arguments]
