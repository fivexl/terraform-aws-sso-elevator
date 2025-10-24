#!/usr/bin/env bash

set -e

cd src
poetry install --no-root
LOG_LEVEL=DEBUG poetry run pytest -q $1
