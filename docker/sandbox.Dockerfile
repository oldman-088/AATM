FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN pip install --no-cache-dir pytest ruff \
    && groupadd --gid 10001 sandbox \
    && useradd --uid 10001 --gid 10001 --create-home --shell /bin/sh sandbox \
    && mkdir -p /workspace \
    && chown 10001:10001 /workspace

USER 10001:10001
WORKDIR /workspace
