#!/bin/bash
set -eu

# Trap signals for graceful shutdown
trap 'echo "Shutting down gracefully..."; exit 0' SIGTERM SIGINT

echo "==> Running database migrations (alembic upgrade head)..."
if ! alembic upgrade head; then
    echo "ERROR: Database migrations failed. Aborting startup." >&2
    exit 1
fi

echo "==> Starting application (APP_ENV=${APP_ENV:-development})..."

if [ "$APP_ENV" = "production" ]; then
    # Production: gunicorn with uvicorn workers.
    # Streamable HTTP is stateless per-request — scale workers to CPU count.
    # GUNICORN_WORKERS env var overrides the default below.
    exec gunicorn app.main:app \
        --worker-class uvicorn.workers.UvicornWorker \
        --workers "${GUNICORN_WORKERS:-4}" \
        --bind 0.0.0.0:8001 \
        --timeout 120 \
        --keep-alive 30 \
        --graceful-timeout 30 \
        --access-logfile - \
        --error-logfile - \
        --log-level info
else
    # Development: uvicorn with auto-reload for rapid iteration
    exec uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload
fi
