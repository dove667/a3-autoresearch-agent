#!/usr/bin/env bash
set -euo pipefail
umask 077
harness_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
export PYTHONPATH="$harness_root/src${PYTHONPATH:+:$PYTHONPATH}"
if [[ "$(uname -s)" == Darwin ]]; then
    # Hold sleep assertions for the runtime's lifetime; allow the display to sleep.
    exec caffeinate -is "$harness_root/.venv/bin/python" -m a3_research_agent.cli.launch --project-root "$harness_root" "$@" </dev/null
fi
exec "$harness_root/.venv/bin/python" -m a3_research_agent.cli.launch --project-root "$harness_root" "$@" </dev/null
