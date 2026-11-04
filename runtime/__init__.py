"""Compatibility imports for source checkouts; implementation lives in src."""

import sys
from pathlib import Path

_source = Path(__file__).resolve().parents[1] / "src"
if str(_source) not in sys.path:
    sys.path.insert(0, str(_source))
