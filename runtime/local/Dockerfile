FROM python:3.11-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends git ripgrep curl patch \
    && rm -rf /var/lib/apt/lists/*
COPY mini /opt/mini
RUN pip install --no-cache-dir /opt/mini 'litellm[mcp]==1.106.6' pytest ruff uv
ENV HOME=/tmp/home MSWEA_SILENT_STARTUP=1 MSWEA_CONFIGURED=true
WORKDIR /workspace/a3
CMD ["sleep", "infinity"]
