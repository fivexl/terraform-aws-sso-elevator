FROM ghcr.io/astral-sh/uv:0.9.5 AS uv

FROM public.ecr.aws/lambda/python:3.13

# Copy uv from the official image
COPY --from=uv /uv /bin/uv

# Enable bytecode compilation for better cold-start performance
ENV UV_COMPILE_BYTECODE=1
ENV UV_NO_INSTALLER_METADATA=1
ENV UV_LINK_MODE=copy
