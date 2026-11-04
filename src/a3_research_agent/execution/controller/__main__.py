"""Stable standard-library entry for JSON requests, watchers and export sealers."""

import sys
from pathlib import Path

# Direct execution also works from a source checkout or unpacked installed wheel.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from a3_research_agent.execution.controller.cli import run

if __name__ == "__main__":
    run()
