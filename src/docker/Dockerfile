FROM ghcr.io/astral-sh/uv:0.9.5 AS uv

FROM public.ecr.aws/lambda/python:3.13

# Copy uv from the official image
COPY --from=uv /uv /bin/uv

# Enable bytecode compilation for better cold-start performance
ENV UV_COMPILE_BYTECODE=1
ENV UV_NO_INSTALLER_METADATA=1
ENV UV_LINK_MODE=copy

# Install layer dependencies (slack_bolt, aws-lambda-powertools)
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=layer/uv.lock,target=/tmp/layer/uv.lock \
    --mount=type=bind,source=layer/pyproject.toml,target=/tmp/layer/pyproject.toml \
    cd /tmp/layer && \
    uv export --frozen --no-emit-workspace --no-dev --no-editable -o requirements.txt && \
    uv pip install --system -r requirements.txt

# Install source dependencies
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=src/uv.lock,target=/tmp/src/uv.lock \
    --mount=type=bind,source=src/pyproject.toml,target=/tmp/src/pyproject.toml \
    cd /tmp/src && \
    uv export --frozen --no-emit-workspace --no-dev --no-editable -o requirements.txt && \
    uv pip install --system -r requirements.txt

# Copy function code
COPY src/ ${LAMBDA_TASK_ROOT}/

CMD ["main.lambda_handler"]
