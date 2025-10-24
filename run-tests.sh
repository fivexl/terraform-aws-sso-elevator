#!/usr/bin/env bash

set -e

pre-commit run ruff -a
pre-commit run black -a
pre-commit run codespell -a

cd src
uv sync --all-extras
LOG_LEVEL=DEBUG uv run pytest -q $1
