#!/usr/bin/env bash
# Start the read-only research dashboard, or report the one already running.
#
#   bash start-dashboard.sh            # start it, or print the URL if it is up
#   bash start-dashboard.sh --open     # also open it in the browser
set -euo pipefail
umask 077
harness_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
export PYTHONPATH="$harness_root/src${PYTHONPATH:+:$PYTHONPATH}"

port="${A3_DASHBOARD_PORT:-8787}"
url="http://127.0.0.1:${port}"

if curl -fsS --max-time 2 "$url/api/health" >/dev/null 2>&1; then
    echo "看板已在运行：$url"
    exit 0
fi

echo "启动看板：$url"
exec "$harness_root/.venv/bin/python" -m a3_research_agent.host.dashboard \
    --project-root "$harness_root" --port "$port" "$@" </dev/null
