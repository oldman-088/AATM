FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/backend

WORKDIR /app
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt \
    && mkdir -p /workspace \
    && chown 65534:65534 /workspace

COPY backend /app/backend

USER 65534:65534
WORKDIR /workspace
CMD ["python", "-m", "app.runtime.worker_daemon"]
