FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git libegl1 libgl1 libglib2.0-0 \
    && git config --system --add safe.directory /opt/a3 \
    && rm -rf /var/lib/apt/lists/*

COPY python /opt/python
COPY a3 /opt/a3
RUN find /opt/a3/.venv/bin -maxdepth 1 -name "python*" -type l -delete \
    && /opt/python/bin/python3.12 -m venv --without-pip /opt/a3/.venv \
    && /opt/a3/.venv/bin/python -m pip install --no-deps --no-build-isolation -e /opt/a3 \
    && printf '#!/bin/sh\nexec /opt/a3/.venv/bin/python -m a3_dual_arm_sim.cli "$@"\n' > /usr/local/bin/a3-sim \
    && chmod +x /usr/local/bin/a3-sim \
    && mkdir -p /workspace

ENV PATH="/opt/a3/.venv/bin:/opt/a3/.runtime/ffmpeg/bin:/opt/python/bin:${PATH}" \
    LD_LIBRARY_PATH="/opt/a3/.runtime/ffmpeg/lib" \
    MUJOCO_GL="egl" \
    MPLCONFIGDIR="/tmp/matplotlib" \
    XDG_CACHE_HOME="/tmp/cache" \
    HF_HUB_OFFLINE="1" \
    TRANSFORMERS_OFFLINE="1" \
    NVIDIA_DRIVER_CAPABILITIES="compute,utility,graphics"
WORKDIR /workspace
