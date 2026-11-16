"""Minimal CPU preflight entry used by the real-Host benchmark."""

import json
import os
import sys


def preflight():
    with open("a3-preflight.json", "w", encoding="utf-8") as stream:
        json.dump({"schema": 1, "status": "passed"}, stream)
    sys.exit(0)


if os.environ.get("A3_RESEARCH_PREFLIGHT") == "1":
    preflight()
print("benchmark preflight entry")
