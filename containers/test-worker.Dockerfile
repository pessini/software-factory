# Native-protocol simulation only; never use as live provider acceptance evidence.
FROM python:3.14-slim-bookworm@sha256:516b51cb9db2ef061e465c803e50f64361b82cefd91e525b889b967b83e83527
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY tests/fixtures/fake_app_server.py /opt/factory/fake_app_server.py
ENTRYPOINT ["python3", "-u", "/opt/factory/fake_app_server.py"]
