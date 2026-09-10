FROM ghcr.io/astral-sh/uv:0.12.12 AS uv
FROM node:24-bookworm
COPY --from=uv /uv /uvx /usr/local/bin/
ENV UV_PYTHON_INSTALL_DIR=/opt/python
RUN apt-get update && apt-get install -y --no-install-recommends ripgrep \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global @openai/codex@0.154.0 \
    && uv python install 3.14.7
ENTRYPOINT ["codex", "app-server"]
