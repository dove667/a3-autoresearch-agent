"""Legacy checkout entry; use a3_research_agent.host.runs.recovery."""

import sys
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from a3_research_agent._compat import redirect

_implementation = redirect(__name__, "a3_research_agent.host.runs.recovery", globals())
