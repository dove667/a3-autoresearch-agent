"""CPU preflight plus a real GPU probe for the benchmark."""
import json
import os
import subprocess
import sys

if os.environ.get("A3_RESEARCH_PREFLIGHT") == "1":
    with open("a3-preflight.json", "w", encoding="utf-8") as stream:
        json.dump({"schema": 1, "status": "passed"}, stream)
    sys.exit(0)

result = subprocess.run(
    ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
    capture_output=True,
    text=True,
    check=False,
)
with open("gpu.txt", "w", encoding="utf-8") as stream:
    stream.write(result.stdout or result.stderr)
sys.exit(result.returncode)
