# Locked controller; the Docker CLI is controller-only, never mounted into workers.
FROM docker:29.1.3-cli@sha256:4fa0ee1f3a7e4354c4ea34558b6d4ee32859baf4973d4c8ccc8e7fe3dd730c04 AS docker
FROM rust:1.99.1@sha256:09027f873d45b8ecd1dc86f1fa13b7a62b875c961f017a0e331267b350430137 AS build
WORKDIR /build
COPY Cargo.toml Cargo.lock rust-toolchain.toml ./
COPY rust/factory-runtime/Cargo.toml rust/factory-runtime/Cargo.toml
COPY rust/factory-runtime/src rust/factory-runtime/src
COPY rust/factory-runtime/migrations rust/factory-runtime/migrations
COPY src/software_factory/web_static src/software_factory/web_static
RUN --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/usr/local/cargo/git \
    --mount=type=cache,target=/build/target \
    cargo build --locked --release -p factory-runtime \
    && cp target/release/factory-runtime /factory-runtime

FROM debian:stable-slim@sha256:13e0a2e78aa0554ad5ff66e4e260c6dbcdff807eb1f39d43b9fb0288aae88b04
ARG FACTORY_UID=10001
ARG FACTORY_GID=10001
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git curl gh \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid ${FACTORY_GID} factory && useradd --uid ${FACTORY_UID} --gid factory --no-create-home factory \
    && mkdir /data /workspace && chown factory:factory /data /workspace
COPY --from=docker /usr/local/bin/docker /usr/local/bin/docker
COPY --from=build /factory-runtime /usr/local/bin/factory-runtime
COPY containers/worker-seccomp.json /opt/factory/worker-seccomp.json
COPY containers/controller-entrypoint.sh /usr/local/bin/controller-entrypoint
USER ${FACTORY_UID}:${FACTORY_GID}
ENTRYPOINT ["controller-entrypoint"]
CMD ["serve"]
