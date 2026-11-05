FROM ghcr.io/astral-sh/uv:0.12.12@sha256:73d2665b478d8fa2de1cf105c6841f8e9cb6b09e568fc7700440c09f8fcd7ac4 AS uv
FROM node:24-bookworm@sha256:994ed664b2644b1b186516daaa1fb7aa93a1e717da2a0ff17094c765d5dbfde1
COPY --from=uv /uv /uvx /usr/local/bin/
ENV UV_PYTHON_INSTALL_DIR=/opt/python
RUN apt-get update && apt-get install -y --no-install-recommends ripgrep \
    && rm -rf /var/lib/apt/lists/* \
    && npm install --global @openai/codex@0.154.0 \
    && uv python install 3.14.7
ENTRYPOINT ["codex", "app-server"]
