#!/usr/bin/env bash

set -e

pre-commit run ruff -a
pre-commit run black -a
pre-commit run codespell -a

cd src
poetry install --no-root
LOG_LEVEL=DEBUG poetry run pytest -q $1
