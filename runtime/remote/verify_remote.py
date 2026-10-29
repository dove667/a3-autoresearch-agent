"""Exercise a deployed controller and image using disposable experiment jobs."""

import argparse
import base64
import concurrent.futures
import json
import subprocess
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--controller", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    records = {}
    jobs = []
    prefix = f"verify-{int(time.time())}"

    def call(operation, request, *, gpu_ids=""):
        command = [
            sys.executable,
            str(args.controller),
            "--root",
            str(args.root),
            "--image",
            args.image,
        ]
        if gpu_ids:
            command += ["--gpu-ids", gpu_ids]
        result = subprocess.run(
            [*command, operation],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        return json.loads(result.stdout)

    def submit(owner, code, gpu=False, *, gpu_ids=""):
        result = call(
            "submit",
            {
                "experiment_id": owner,
                "command": ["python", "probe.py"],
                "gpu": gpu,
                "files": {"probe.py": code},
            },
            gpu_ids=gpu_ids,
        )
        if result.get("job_id"):
            jobs.append((owner, result["job_id"]))
        return result

    def finish(owner, job):
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            result = call("status", {"experiment_id": owner, "job_id": job["job_id"]})
            if result["status"] not in {"created", "running", "restarting"}:
                return result
            time.sleep(0.25)
        raise TimeoutError(job["job_id"])

    try:
        records["health"] = call("health", {})
        assert records["health"]["status"] == "ok"
        owner_a, owner_b = f"{prefix}-a", f"{prefix}-b"
        first = submit(
            owner_a,
            "from pathlib import Path; Path('sentinel.txt').write_text('owner-a'); Path('link').symlink_to('/opt/a3/README.md')",
        )
        assert first["status"] == "created", first
        result = finish(owner_a, first)
        assert result["status"] == "completed", result
        sentinel = args.root / "experiments" / owner_a / first["job_id"] / "sentinel.txt"
        probe = f"""from pathlib import Path
import errno
import torch
assert torch.cuda.device_count() == 0
assert not Path('sentinel.txt').exists()
assert not Path({str(sentinel)!r}).exists()
assert not Path('/var/run/docker.sock').exists()
try:
    Path('/opt/a3/readonly-probe').write_text('blocked')
except OSError as error:
    assert error.errno in (errno.EROFS, errno.EACCES)
else:
    raise AssertionError('baseline is writable')
print('experiment isolation passed')
"""
        second = submit(owner_b, probe)
        result = finish(owner_b, second)
        assert result["status"] == "completed", result
        records["isolation"] = result
        artifact = call(
            "artifact",
            {"experiment_id": owner_a, "job_id": first["job_id"], "path": "sentinel.txt"},
        )
        assert base64.b64decode(artifact["data"]) == b"owner-a"
        for operation in ("status", "cancel", "artifact"):
            denied = call(
                operation,
                {"experiment_id": owner_b, "job_id": first["job_id"], "path": "sentinel.txt"},
            )
            assert denied == {
                "status": "error",
                "error": "PermissionError",
                "message": "job is not accessible",
            }, denied
        linked = call(
            "artifact", {"experiment_id": owner_a, "job_id": first["job_id"], "path": "link"}
        )
        assert linked["status"] == "error" and linked["error"] == "ValueError", linked
        records["ownership_and_artifacts"] = "passed"

        gpu = submit(
            f"{prefix}-gpu",
            """import torch
assert torch.cuda.is_available()
assert torch.cuda.device_count() == 1
x = torch.randn(32, 32, device='cuda', requires_grad=True)
(x @ x.T).square().mean().backward()
assert torch.isfinite(x.grad).all()
print(torch.__version__, torch.cuda.get_device_name(0), 'CUDA passed')
""",
            True,
        )
        assert gpu["status"] == "created", gpu
        result = finish(f"{prefix}-gpu", gpu)
        assert result["status"] == "completed", result
        records["gpu"] = result
        render = submit(
            f"{prefix}-render",
            """import json
import subprocess
from pathlib import Path
from PIL import Image
subprocess.run(['a3-sim', 'smoke', '--steps', '20'], check=True)
subprocess.run(['python', '-m', 'a3_dual_arm_sim.workflows.preview_cameras_cli', '--output', '/workspace/cameras'], check=True)
for name in ('front', 'left_wrist', 'right_wrist'):
    image = Image.open(Path('cameras') / (name + '.png'))
    assert image.size == (256, 256)
print('EGL cameras passed')
""",
            True,
        )
        result = finish(f"{prefix}-render", render)
        assert result["status"] == "completed", result
        records["smoke_and_cameras"] = result
        camera = call(
            "artifact",
            {
                "experiment_id": f"{prefix}-render",
                "job_id": render["job_id"],
                "path": "cameras/front.png",
            },
        )
        assert base64.b64decode(camera["data"]).startswith(b"\x89PNG\r\n\x1a\n")
        records["camera_artifact"] = "passed"

        gpu_data = subprocess.run(
            ["nvidia-smi", "--query-gpu=index,uuid", "--format=csv,noheader"],
            text=True,
            capture_output=True,
            check=True,
        ).stdout
        physical_index = next(
            line.split(",")[0].strip() for line in gpu_data.splitlines() if gpu["gpu_uuid"] in line
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    submit,
                    f"{prefix}-lock-{index}",
                    "import time; time.sleep(12)",
                    True,
                    gpu_ids=physical_index,
                )
                for index in range(2)
            ]
            lock_results = [future.result() for future in futures]
        assert sorted(result["status"] for result in lock_results) == [
            "created",
            "resource_busy",
        ], lock_results
        winner_index = next(
            index for index, result in enumerate(lock_results) if result.get("job_id")
        )
        winner = lock_results[winner_index]
        cancelled = call(
            "cancel", {"experiment_id": f"{prefix}-lock-{winner_index}", "job_id": winner["job_id"]}
        )
        assert cancelled["status"] == "cancelled", cancelled
        finish(f"{prefix}-lock-{winner_index}", winner)
        records["concurrent_gpu_lock"] = lock_results

        info = json.loads(
            subprocess.run(
                ["docker", "inspect", f"a3-job-{second['job_id']}"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout
        )[0]
        config = info["HostConfig"]
        assert config["NetworkMode"] == "none" and config["ReadonlyRootfs"]
        assert config["Memory"] == 8 * 1024**3 and config["NanoCpus"] == 2 * 10**9
        assert config["PidsLimit"] == 256 and config["CapDrop"] == ["ALL"]
        assert len(info["Mounts"]) == 1 and info["Mounts"][0]["Destination"] == "/workspace"
        records["container_limits"] = "passed"
    finally:
        for owner, job_id in jobs:
            call("cancel", {"experiment_id": owner, "job_id": job_id})
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(records, indent=2))
    print(json.dumps(records, indent=2))


if __name__ == "__main__":
    main()
