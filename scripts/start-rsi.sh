#!/usr/bin/env bash
# Prepare one unattended upstream-mini run; harness evolution belongs to the agent.
set -euo pipefail
umask 077

harness_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
env_file=${A3_RSI_ENV_FILE:-"$harness_root/.env"}
if [[ -f "$env_file" ]]; then
    set -a
    source "$env_file"
    set +a
fi

case "${1:-}" in
    ""|--check) ;;
    *) echo 'Usage: bash scripts/start-rsi.sh [--check]' >&2; exit 2 ;;
esac

: "${MODEL_NAME:?Set MODEL_NAME in the environment or .env}"
: "${MODEL_CLASS:?Set MODEL_CLASS in the environment or .env}"
: "${A3_PROJECT_ROOT:?Set A3_PROJECT_ROOT to the A3 experiment worktree}"
mini="$harness_root/.venv/bin/mini"
python="$harness_root/.venv/bin/python"
test -x "$mini"
test -x "$python"
A3_PROJECT_ROOT=$(cd -- "$A3_PROJECT_ROOT" && pwd -P)
export A3_PROJECT_ROOT
export A3_HARNESS_ROOT="$harness_root"
export MSWEA_CONFIGURED=true MSWEA_SILENT_STARTUP=1
export MSWEA_GLOBAL_CALL_LIMIT=0 MSWEA_GLOBAL_COST_LIMIT=0

git -C "$harness_root" rev-parse --verify HEAD >/dev/null
git -C "$A3_PROJECT_ROOT" rev-parse --verify HEAD >/dev/null
"$python" - <<'PY'
import os
from minisweagent.models import get_model_class
get_model_class(os.environ['MODEL_NAME'], os.environ['MODEL_CLASS'])
PY
if [[ -n "${A3_REMOTE_HOST:-}" ]]; then
    : "${A3_REMOTE_PROJECT_ROOT:?Set the remote project path}"
    : "${A3_REMOTE_ACTIVATE:?Set the remote environment activation path}"
    : "${A3_REMOTE_RUN_ROOT:?Set the remote run directory}"
    ssh -o BatchMode=yes -o ConnectTimeout=10 "$A3_REMOTE_HOST" true </dev/null
fi
if [[ "${1:-}" == --check ]]; then
    echo 'Local entry point, Git baselines, model adapter and SSH authentication checked.'
    echo 'This check does not call the model or run the RSI experiment.'
    exit 0
fi

mkdir -p "$harness_root/artifacts/rsi"
A3_RUN_DIR=$(mktemp -d "$harness_root/artifacts/rsi/run-$(date -u +%Y%m%dT%H%M%SZ)-XXXXXX")
export A3_RUN_DIR
export A3_GOAL_FILE="$A3_RUN_DIR/goal.md"
export A3_ACCEPTANCE_FILE="$A3_RUN_DIR/acceptance.md"
export A3_DEPLOYMENT_FILE="$A3_RUN_DIR/deployment.json"
cp "$harness_root/evals/goal.md" "$A3_GOAL_FILE"
cp "$harness_root/evals/acceptance.md" "$A3_ACCEPTANCE_FILE"
cp "$harness_root/configs/rsi-bootstrap.yaml" "$A3_RUN_DIR/bootstrap.yaml"
"$python" - <<'PY'
import hashlib
import json
import os
from pathlib import Path
import subprocess

run = Path(os.environ['A3_RUN_DIR'])
deployment_keys = (
    'A3_PROJECT_ROOT', 'A3_HARNESS_ROOT', 'A3_REMOTE_HOST',
    'A3_REMOTE_PROJECT_ROOT', 'A3_REMOTE_ACTIVATE', 'A3_REMOTE_RUN_ROOT',
)
(run / 'deployment.json').write_text(json.dumps(
    {key: os.environ.get(key) for key in deployment_keys}, indent=2) + '\n')
snapshot = {'model': os.environ['MODEL_NAME'], 'model_class': os.environ['MODEL_CLASS']}
snapshot['launcher_pid'] = os.getppid()
snapshot['inputs'] = {name: hashlib.sha256((run / name).read_bytes()).hexdigest()
                      for name in ('goal.md', 'acceptance.md', 'bootstrap.yaml')}
snapshot['repositories'] = {}
repositories = {
    'a3': os.environ['A3_PROJECT_ROOT'],
    'harness': os.environ['A3_HARNESS_ROOT'],
    'mini': str(Path(os.environ['A3_HARNESS_ROOT']) / 'third_party/mini-swe-agent'),
}
for name, root in repositories.items():
    def git(*args):
        return subprocess.check_output(['git', '-C', root, *args], text=True)
    (run / f'{name}-start.patch').write_text(git('diff', 'HEAD', '--binary'))
    (run / f'{name}-start-status.txt').write_text(git('status', '--porcelain'))
    snapshot['repositories'][name] = {
        'commit': git('rev-parse', 'HEAD').strip(),
        'branch': git('branch', '--show-current').strip(),
    }
(run / 'startup.json').write_text(json.dumps(snapshot, indent=2) + '\n')
PY
printf '%s\n' "$A3_RUN_DIR" > "$harness_root/artifacts/rsi/latest-run.txt"
printf 'Run directory: %s\n' "$A3_RUN_DIR"
printf 'Trajectory: %s/trajectory.json\n' "$A3_RUN_DIR"
cd -- "$A3_PROJECT_ROOT"
exec "$mini" --agent-class default --environment-class local \
    --model "$MODEL_NAME" --model-class "$MODEL_CLASS" \
    -c "$A3_RUN_DIR/bootstrap.yaml" \
    -c "model.cost_tracking=${MODEL_COST_TRACKING:-default}" \
    --output "$A3_RUN_DIR/trajectory.json" \
    </dev/null >"$A3_RUN_DIR/console.log" 2>&1
