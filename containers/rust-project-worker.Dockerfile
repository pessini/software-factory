# Repository-specific worker: the product/controller remains independent of Rust tooling.
FROM ghcr.io/astral-sh/uv:0.12.12@sha256:73d2665b478d8fa2de1cf105c6841f8e9cb6b09e568fc7700440c09f8fcd7ac4 AS uv
FROM node:24-bookworm@sha256:994ed664b2644b1b186516daaa1fb7aa93a1e717da2a0ff17094c765d5dbfde1 AS provider
RUN npm install --global @openai/codex@0.154.0

FROM rust:1.99.1@sha256:09027f873d45b8ecd1dc86f1fa13b7a62b875c961f017a0e331267b350430137 AS dependencies
ENV CARGO_BUILD_JOBS=1 CARGO_INCREMENTAL=0 CARGO_PROFILE_DEV_DEBUG=0 CARGO_PROFILE_TEST_DEBUG=0
WORKDIR /build
COPY Cargo.toml Cargo.lock rust-toolchain.toml ./
COPY rust/factory-runtime rust/factory-runtime
# Download and compile once at image build time. Quality containers have no network.
RUN cargo test --workspace --locked --no-run

FROM rust:1.99.1@sha256:09027f873d45b8ecd1dc86f1fa13b7a62b875c961f017a0e331267b350430137
RUN apt-get update && apt-get install -y --no-install-recommends python3 ripgrep \
    && rm -rf /var/lib/apt/lists/*
COPY --from=provider /usr/local/bin/node /usr/local/bin/node
COPY --from=provider /usr/local/lib/node_modules/@openai /usr/local/lib/node_modules/@openai
RUN ln -s ../lib/node_modules/@openai/codex/bin/codex.js /usr/local/bin/codex
COPY --from=uv /uv /uvx /usr/local/bin/
COPY --from=dependencies /usr/local/rustup /usr/local/rustup
COPY --from=dependencies /usr/local/cargo/registry /opt/factory-rust-cache/cargo/registry
COPY --from=dependencies /usr/local/cargo/git /opt/factory-rust-cache/cargo/git
COPY --from=dependencies /build/target /opt/factory-rust-cache/target
COPY containers/factory-rust-check.sh /usr/local/bin/factory-rust-check
RUN chmod 755 /usr/local/bin/factory-rust-check \
    && chmod -R a+rX /opt/factory-rust-cache
ENTRYPOINT ["codex", "app-server"]
