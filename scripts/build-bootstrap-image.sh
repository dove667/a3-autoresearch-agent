#!/usr/bin/env bash
set -euo pipefail
harness_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
env_file=${A3_RSI_ENV_FILE:-"$harness_root/.env"}
if [[ -f "$env_file" ]]; then
    set -a
    source "$env_file"
    set +a
fi
context=$(mktemp -d)
trap 'rm -rf -- "$context"' EXIT
"$harness_root/.venv/bin/python" - "$harness_root" "$context" <<'PY'
from pathlib import Path
import shutil
import sys
import subprocess
root, context = map(Path, sys.argv[1:])
shutil.copy2(root / 'runtime/local/Dockerfile', context / 'Dockerfile')
mini = root / 'third_party/mini-swe-agent'
files = subprocess.check_output(['git', '-C', str(mini), 'ls-files', '-z',
                                 '--cached', '--others', '--exclude-standard']).decode().split('\0')
for name in filter(None, files):
    source = mini / name
    if source.is_file() and not source.is_symlink():
        target = context / 'mini' / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
PY
docker build -t "${A3_LOCAL_IMAGE:-a3-agent-bootstrap:local}" "$context"
