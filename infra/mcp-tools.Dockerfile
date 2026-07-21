# Nightingale L4 MCP tool servers (mock-backed) — one image, run per-tool over streamable-HTTP.
# Foundry hosted agents attach to these as hosted MCP tool endpoints (server_url .../mcp).
# The Container App sets args=["<tool>", "--http"] so the same image serves any one tool.
#
# Build in the cloud (no local Docker needed):
#   az acr build -r <acr> -t nightingale-mcp-tools:<tag> -f infra/mcp-tools.Dockerfile .
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

WORKDIR /app

# Slim dependency set — just what the L4 tools import (mcp + config + telemetry API).
# Avoids the heavy voice/agent-framework/pyaudio deps that the tool servers never use.
COPY infra/requirements-mcp-tools.txt ./requirements-mcp-tools.txt
RUN pip install --no-cache-dir -r requirements-mcp-tools.txt

# Only the code the tool servers need at runtime.
COPY config.py ./config.py
COPY src/__init__.py ./src/__init__.py
COPY src/tools ./src/tools
COPY src/telemetry ./src/telemetry

EXPOSE 8080

# ENTRYPOINT is the tool launcher; the Container App supplies ["<tool>", "--http"] as args.
ENTRYPOINT ["python", "-m", "src.tools"]
CMD ["oncall_lookup", "--http"]
