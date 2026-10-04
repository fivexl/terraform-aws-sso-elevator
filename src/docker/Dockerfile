FROM ghcr.io/astral-sh/uv:0.12.17@sha256:10787c682e4184e4f290de1171fd4703dc63de99221f10fe1c99002ce7fa9acc AS uv

FROM public.ecr.aws/lambda/python:3.14@sha256:76cca2fae40215054fbe9fb922ac58aeeff727a6773aaf9d8a912b40ffab6460

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

# Copy function code with proper ownership for non-root user
COPY --chown=1000:1000 src/ ${LAMBDA_TASK_ROOT}/

# Run as non-root user for security
USER 1000

CMD ["main.lambda_handler"]
