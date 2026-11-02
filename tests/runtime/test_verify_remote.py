"""Check that deployment verification rejects software EGL fallbacks."""

import sys
from types import ModuleType

import pytest

from runtime.remote import verify_remote


@pytest.mark.parametrize(
    ("vendor", "renderer", "accepted"),
    [
        (b"NVIDIA Corporation", b"NVIDIA GeForce RTX /PCIe/SSE2", True),
        (b"Mesa", b"llvmpipe (LLVM)", False),
        (b"NVIDIA Corporation", b"llvmpipe", False),
        (None, None, False),
    ],
)
def test_egl_probe_checks_current_context_and_releases_it(
    monkeypatch, vendor, renderer, accepted, capsys
):
    events = []
    mujoco = ModuleType("mujoco")

    class Context:
        def __init__(self, width, height):
            assert (width, height) == (16, 16)

        def make_current(self):
            events.append("current")

        def free(self):
            events.append("freed")

    mujoco.GLContext = Context
    opengl = ModuleType("OpenGL")
    gl = ModuleType("OpenGL.GL")
    gl.GL_VENDOR, gl.GL_RENDERER = 1, 2

    def get_string(field):
        assert events == ["current"]
        return vendor if field == gl.GL_VENDOR else renderer

    gl.glGetString = get_string
    opengl.GL = gl
    monkeypatch.setitem(sys.modules, "mujoco", mujoco)
    monkeypatch.setitem(sys.modules, "OpenGL", opengl)
    monkeypatch.setitem(sys.modules, "OpenGL.GL", gl)
    if accepted:
        exec(verify_remote.NVIDIA_EGL_PROBE, {})  # noqa: S102 - repository-owned job probe
        assert "NVIDIA GeForce RTX" in capsys.readouterr().out
    else:
        with pytest.raises(RuntimeError, match="NVIDIA hardware EGL required"):
            exec(verify_remote.NVIDIA_EGL_PROBE, {})  # noqa: S102 - repository-owned job probe
    assert events == ["current", "freed"]
