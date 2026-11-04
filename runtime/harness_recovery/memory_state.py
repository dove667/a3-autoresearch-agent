"""Legacy checkout adapter; use the canonical source package."""

import sys
from pathlib import Path

_source = Path(__file__).resolve().parents[2] / "src"
if str(_source) not in sys.path:
    sys.path.insert(0, str(_source))

from a3_research_agent._compat import redirect

_implementation = redirect(__name__, "a3_research_agent.harness.memory.state", globals())
