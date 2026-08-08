#!/usr/bin/env bash
set -euo pipefail

uv sync
software-factory repo-check
