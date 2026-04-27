FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /opt/ax-cli

COPY pyproject.toml README.md ./
COPY ax_cli ./ax_cli

RUN pip install --no-cache-dir .

ENTRYPOINT []
