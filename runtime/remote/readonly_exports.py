"""Compatibility entry point; implementation lives in a3_research_agent.execution."""

import sys
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from a3_research_agent._compat import redirect

_implementation = redirect(__name__, "a3_research_agent.execution.exports.readonly", globals())
