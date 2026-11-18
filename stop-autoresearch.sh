#!/usr/bin/env bash
# Ask the running research process to stop after its current turn.
#
#   bash stop-autoresearch.sh                  # newest run
#   bash stop-autoresearch.sh trajectories/2026-11-18_11-30-00-000000
#   bash stop-autoresearch.sh --reason "moving the laptop"
#   bash stop-autoresearch.sh --force ...      # even without a live process
set -euo pipefail
umask 077
harness_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
export PYTHONPATH="$harness_root/src${PYTHONPATH:+:$PYTHONPATH}"

run=""
force=0
arguments=()
for argument in "$@"; do
    case "$argument" in
        --force) force=1 ;;
        -h|--help)
            sed -n '2,7p' "${BASH_SOURCE[0]}" | sed 's/^# \\?//'
            exit 0
            ;;
        *)
            if [[ -z "$run" && "$argument" != -* ]]; then
                run="$argument"
            else
                arguments+=("$argument")
            fi
            ;;
    esac
done

if [[ -z "$run" ]]; then
    run=$(find "$harness_root/trajectories" -mindepth 1 -maxdepth 1 -type d -print 2>/dev/null \
        | while read -r candidate; do printf '%s\t%s\n' "$(stat -f '%m' "$candidate")" "$candidate"; done \
        | sort -rn | head -1 | cut -f2-)
fi
if [[ -z "$run" || ! -d "$run/conversation" ]]; then
    echo "No run with a conversation directory: ${run:-none}" >&2
    exit 2
fi

if ! pgrep -f "a3_research_agent.harness.agent" >/dev/null 2>&1; then
    if [[ "$force" -ne 1 ]]; then
        echo "No research process is running. Nothing to stop." >&2
        echo "A request left behind would stop the next start immediately; pass --force to write it anyway." >&2
        exit 1
    fi
    echo "No research process found; writing the request because --force was given." >&2
fi

exec "$harness_root/.venv/bin/python" -m a3_research_agent.cli.request_stop "$run" "${arguments[@]}" </dev/null
