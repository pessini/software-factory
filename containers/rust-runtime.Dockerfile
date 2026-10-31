# Development-only fake-worker slice. Production packaging is tracked in #19.
FROM rust:1.99.1@sha256:09027f873d45b8ecd1dc86f1fa13b7a62b875c961f017a0e331267b350430137 AS build
WORKDIR /build
COPY Cargo.toml Cargo.lock rust-toolchain.toml ./
COPY rust/factory-runtime rust/factory-runtime
RUN cargo build --locked --release -p factory-runtime

FROM debian:stable-slim@sha256:13e0a2e78aa0554ad5ff66e4e260c6dbcdff807eb1f39d43b9fb0288aae88b04
RUN groupadd --gid 10001 factory && useradd --uid 10001 --gid factory --no-create-home factory \
    && mkdir /data /workspace && chown factory:factory /data /workspace
COPY --from=build /build/target/release/factory-runtime /usr/local/bin/factory-runtime
USER 10001:10001
VOLUME ["/data", "/workspace"]
ENTRYPOINT ["factory-runtime", "/data", "/workspace"]
CMD ["serve"]
