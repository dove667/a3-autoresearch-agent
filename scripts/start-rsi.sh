#!/usr/bin/env bash
set -euo pipefail
umask 077
harness_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
export MSWEA_CONFIGURED=true MSWEA_SILENT_STARTUP=1
export MSWEA_GLOBAL_CALL_LIMIT=0 MSWEA_GLOBAL_COST_LIMIT=0
export PYTHONPATH="$harness_root${PYTHONPATH:+:$PYTHONPATH}"
if [[ "$(uname -s)" == Darwin ]]; then
    # Hold sleep assertions for the runtime's lifetime; allow the display to sleep.
    exec caffeinate -is "$harness_root/.venv/bin/python" "$harness_root/runtime/local/launch.py" "$@" </dev/null
fi
exec "$harness_root/.venv/bin/python" "$harness_root/runtime/local/launch.py" "$@" </dev/null
