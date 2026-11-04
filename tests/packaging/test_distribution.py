"""Build real artifacts and execute them without checkout import paths."""

import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def distributions(tmp_path_factory):
    output = tmp_path_factory.mktemp("distributions")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import setuptools.build_meta as b; output=sys.argv[1]; sys.argv=['setup.py']; b.build_sdist(output); b.build_wheel(output)",
            str(output),
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return output


def test_artifacts_contain_controller_resources_and_no_private_state(distributions):
    wheel = next(distributions.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert "a3_research_agent/execution/controller/nvidia_egl_vendor.json" in names
        assert "a3_research_agent/execution/images/Dockerfile" in names
        assert not any(
            part in {".env", "artifacts", ".git", "AGENTS.md"}
            for name in names
            for part in Path(name).parts
        )
    with tarfile.open(next(distributions.glob("*.tar.gz"))) as archive:
        assert any(name.endswith("execution/images/Dockerfile") for name in archive.getnames())


def test_installed_wheel_runs_stdlib_controller_and_exports_complete_harness(
    distributions, tmp_path
):
    installed = tmp_path / "installed"
    result = subprocess.run(
        [
            shutil.which("uv") or "uv",
            "pip",
            "install",
            "--no-deps",
            "--no-cache",
            "--target",
            str(installed),
            str(next(distributions.glob("*.whl"))),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    entry = installed / "a3_research_agent/execution/controller/__main__.py"
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(entry), "--help"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    script = """
import json, pathlib, sys
sys.path.insert(0, sys.argv[1])
import a3_research_agent
from a3_research_agent.packaging.harness_payload import build_harness_payload
from a3_research_agent.project import checkout_root
files = build_harness_payload()
print(json.dumps(dict(package=a3_research_agent.__file__, root=checkout_root(), files=sorted(files))))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", script, str(installed)],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    record = json.loads(result.stdout)
    assert Path(record["package"]).is_relative_to(installed)
    assert record["root"] is None
    assert "agent.py" in record["files"]
    assert "a3_research_agent/harness/orchestration/dispatch.py" in record["files"]

    host_script = "import sys; sys.path.insert(0, sys.argv.pop(1)); from a3_research_agent.cli.launch import main; main()"
    for flags, expected in ((["--help"], 0), ([], 2)):
        result = subprocess.run(
            [sys.executable, "-I", "-c", host_script, str(installed), *flags],
            cwd=tmp_path,
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
            env={
                **os.environ,
                "PYTHON_DOTENV_DISABLED": "1",
                "MSWEA_GLOBAL_CONFIG_DIR": str(tmp_path / "mini-config"),
                "LITELLM_LOCAL_MODEL_COST_MAP": "True",
                "MSWEA_SILENT_STARTUP": "1",
            },
        )
        assert result.returncode == expected, result.stderr
        assert "--project-root" in result.stdout + result.stderr


def test_sdist_rebuilds_the_same_package_sources(distributions, tmp_path):
    with tarfile.open(next(distributions.glob("*.tar.gz"))) as archive:
        for member in archive.getmembers():
            target = tmp_path / member.name
            assert target.resolve().is_relative_to(tmp_path.resolve())
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                assert member.isfile()
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source:
                    target.write_bytes(source.read())
    source_root = next(path for path in tmp_path.iterdir() if path.is_dir())
    rebuilt = tmp_path / "rebuilt"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import setuptools.build_meta as b; output=sys.argv[1]; sys.argv=['setup.py']; b.build_wheel(output)",
            str(rebuilt),
        ],
        cwd=source_root,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert result.returncode == 0, result.stderr
    with (
        zipfile.ZipFile(next(distributions.glob("*.whl"))) as original,
        zipfile.ZipFile(next(rebuilt.glob("*.whl"))) as other,
    ):
        expected = {
            name: original.read(name)
            for name in original.namelist()
            if name.startswith("a3_research_agent/")
        }
        actual = {
            name: other.read(name)
            for name in other.namelist()
            if name.startswith("a3_research_agent/")
        }
    assert actual == expected
