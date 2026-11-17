"""Write a tiny output file for the real export benchmark."""
from pathlib import Path

Path("probe.txt").write_text("probe-ok\n", encoding="utf-8")
