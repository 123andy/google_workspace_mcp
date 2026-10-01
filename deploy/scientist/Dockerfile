# Scientist.com production image: remote, multi-user (streamable HTTP). Built by
# .github/workflows/scientist-image.yaml from the checked-out tree; the repo is private, so nothing
# is fetched from GitHub at build time.
#
# Not the root Dockerfile: upstream's runs `uv run main.py` under /bin/sh, while the deployment's
# per-endpoint args assume ENTRYPOINT `workspace-mcp --transport streamable-http`.
#
# Build from the repo root: docker build -f deploy/scientist/Dockerfile .

FROM python:3.12-slim

# requirements.txt pins every dependency (constraints only): the project declares minimum
# versions and uv.lock doesn't match our runtime, so an unpinned build of the same commit drifts.
# The [postgres] extra is the Postgres KV backend.
COPY . /src
RUN pip install --no-cache-dir -c /src/deploy/scientist/requirements.txt "/src[postgres]" \
    && rm -rf /src

RUN groupadd -r mcp && useradd -r -g mcp -d /home/mcp -s /sbin/nologin mcp \
    && mkdir -p /home/mcp && chown -R mcp:mcp /home/mcp
USER mcp

ENV WORKSPACE_MCP_PORT=8000
EXPOSE 8000

# ENTRYPOINT is fixed; CMD is overridden per endpoint. Read-only is the safe bare default.
ENTRYPOINT ["workspace-mcp", "--transport", "streamable-http"]
CMD ["--read-only"]
