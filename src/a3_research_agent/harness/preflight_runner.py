"""Run opt-in GPU and output checks inside the Controller job environment."""

import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path

CUDA_TIMEOUT_SECONDS = 120
CUDA_SMOKE = """\
import torch

value = torch.ones(1, device="cuda")
(value + 1).item()
"""
LOCK_DIRECTORY = ".a3-output-locks"


def guard_output_path(root, path):
    target = root
    for part in Path(path).parts:
        if target.exists() and not target.is_dir():
            raise ValueError(f"unsafe_output_path: a parent of {path!r} is a file")
        target /= part
        if target.is_symlink():
            raise ValueError(f"unsafe_output_path: symlink in {path!r}; choose a fresh output path")
    return target


def lock_outputs(stack, root, paths):
    directory = guard_output_path(root, LOCK_DIRECTORY)
    directory.mkdir(exist_ok=True, mode=0o700)
    namespaces = sorted({Path(path).parts[0] for path in paths})
    for name in namespaces:
        digest = hashlib.sha256(name.encode()).hexdigest()
        fd = os.open(directory / digest, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        stack.callback(os.close, fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(
                f"output_collision: namespace {name!r} is in use; choose a separate output namespace"
            ) from None


def run_probe(command, env, device):
    if device == "cpu":
        return subprocess.run(command, env=env, check=False).returncode
    deadline = time.monotonic() + CUDA_TIMEOUT_SECONDS
    for smoke_check, argv in ((True, [sys.executable, "-c", CUDA_SMOKE]), (False, command)):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("cuda_preflight_timeout: keep the CUDA preflight under 120 seconds")
        # The probe and any workers share a process group so they are stopped together.
        process = subprocess.Popen(argv, env=env, start_new_session=True)
        try:
            code = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise ValueError(
                "cuda_preflight_timeout: keep the CUDA preflight under 120 seconds"
            ) from None
        if code != 0:
            if smoke_check:
                print(
                    "cuda_preflight_unavailable: PyTorch could not execute a CUDA tensor operation",
                    file=sys.stderr,
                )
            return code
    return 0


def run(mode, contract, command):
    output_paths = contract.get("output_paths", [])
    with ExitStack() as stack:
        if output_paths:
            root = Path(os.environ["A3_OUTPUTS"])
            if root.is_symlink():
                raise ValueError("unsafe_output_path: A3_OUTPUTS itself is a symlink")
            root = root.resolve(strict=True)
            lock_outputs(stack, root, output_paths)
            for path in output_paths:
                if guard_output_path(root, path).exists():
                    raise ValueError(
                        f"output_collision: {path!r} already exists under A3_OUTPUTS; choose a fresh path"
                    )
        if mode != "production":
            env = {**os.environ, "A3_RESEARCH_PREFLIGHT": "1", "A3_RESEARCH_PREFLIGHT_DEVICE": mode}
            return run_probe(command, env, mode)
        code = subprocess.run(command, check=False).returncode
        if code == 0:
            for path in contract.get("checkpoint_paths", []):
                target = guard_output_path(root, path)
                if not (target.is_file() or target.is_dir()):
                    raise ValueError(
                        f"checkpoint_missing: training did not produce {path!r} under A3_OUTPUTS"
                    )
        return code


def main(argv):
    mode, contract, *command = argv
    try:
        code = run(mode, json.loads(contract), command)
        return 128 - code if code < 0 else code
    except (OSError, ValueError) as error:
        print(error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
