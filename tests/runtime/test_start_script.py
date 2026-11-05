import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_start_script_preserves_arguments_stdin_and_exit_status_with_sleep_guard(tmp_path, system):
    root = tmp_path / "project with spaces"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    source = Path(__file__).resolve().parents[2] / "scripts/start-rsi.sh"
    shutil.copy2(source, scripts / "start-rsi.sh")
    python = root / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    launcher = root / "src/a3_research_agent/cli/launch.py"
    launcher.parent.mkdir(parents=True)
    launcher.write_text(
        "import json, sys\n"
        "print(json.dumps({'args': sys.argv[1:], 'stdin': sys.stdin.read()}))\n"
        "sys.exit(7)\n"
    )
    commands = tmp_path / "commands"
    commands.mkdir()
    uname = commands / "uname"
    uname.write_text(f"#!/bin/bash\nprintf '%s\\n' '{system}'\n")
    uname.chmod(0o755)
    guard = commands / "caffeinate"
    guard.write_text(
        '#!/bin/bash\n[[ "$1" == "-is" ]] || exit 93\n'
        'printf enabled > "$GUARD_MARKER"\nshift\nexec "$@"\n'
    )
    guard.chmod(0o755)
    marker = tmp_path / "guard-enabled"
    result = subprocess.run(
        ["/bin/bash", str(scripts / "start-rsi.sh"), "--run-harness", "run with spaces"],
        env={**os.environ, "PATH": f"{commands}:{os.defpath}", "GUARD_MARKER": str(marker)},
        input="must not reach the unattended launcher",
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    assert result.returncode == 7, result.stderr
    assert json.loads(result.stdout) == {
        "args": ["--project-root", str(root), "--run-harness", "run with spaces"],
        "stdin": "",
    }
    assert marker.exists() == (system == "Darwin")
