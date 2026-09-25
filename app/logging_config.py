"""
Structured Logging Configuration

Environment-aware logging setup:
  - Production: JSON logs with rich context (request_id, user_id, extra fields)
                Compatible with log aggregation (Datadog, Loki, CloudWatch)
  - Development: Human-readable colored logs to stdout

Usage: Call configure_logging() once at app startup.
"""

import logging
import sys
from datetime import UTC

from app.config import settings


def configure_logging():
    """
    Configure application-wide logging based on APP_ENV.

    Production: JSON structured logs (parseable by aggregation tools)
    Development: Human-readable colored logs (easy to read in terminal)
    """
    is_prod = settings.APP_ENV == "production"
    level = logging.INFO if is_prod else logging.DEBUG

    # Configure root logger
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    # Stream handler to stdout (works in Docker)
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)

    # Select formatter based on environment
    if is_prod:
        formatter = _JsonFormatter()
    else:
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )

    handler.setFormatter(formatter)
    root.addHandler(handler)

    # Suppress noisy third-party loggers
    _configure_third_party_loggers()

    logging.getLogger(__name__).info(
        "Logging configured (env=%s, level=%s)", settings.APP_ENV, logging.getLevelName(level)
    )


def _configure_third_party_loggers():
    """Suppress verbose output from third-party libraries."""
    noisy_loggers = [
        "httpx",  # HTTP client
        "httpcore",  # HTTP core
        "google",  # Google SDKs
        "google.auth",  # Google auth
        "google.ads",  # Google Ads API
        "urllib3",  # Requests HTTP
        "asyncio",  # Async event loop
        "sqlalchemy.engine",  # SQLAlchemy queries
        "watchfiles",  # File watcher (uvicorn reload)
    ]
    for logger_name in noisy_loggers:
        logging.getLogger(logger_name).setLevel(logging.WARNING)


class _JsonFormatter(logging.Formatter):
    """
    Production JSON log formatter for log aggregation tools.

    Output format (per line):
      {
        "ts": "2026-04-18T10:30:45.123Z",
        "level": "INFO",
        "logger": "app.main",
        "msg": "Request processed",
        "module": "main",
        "func": "startup",
        "user_id": "user-123",          # from ContextVar if available
        "extra": {...},                 # custom fields from logger.info(..., extra={...})
        "exc": "Traceback..."           # exception info if present
      }

    Compatible with Datadog, Loki, CloudWatch, Splunk, etc.
    """

    # Standard LogRecord attributes (not logged as "extra")
    _STANDARD_ATTRS = {
        "name",
        "msg",
        "args",
        "created",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "exc_info",
        "exc_text",
        "message",
        "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        import json
        from datetime import datetime

        # Build base log entry
        log_entry = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "module": record.module,
            "func": record.funcName,
        }

        # Add user context from ContextVar if available
        try:
            from app.app_state import current_user_ctx

            user_ctx = current_user_ctx.get(None)
            if user_ctx and hasattr(user_ctx, "user_id"):
                log_entry["user_id"] = str(user_ctx.user_id)
        except Exception:
            # ContextVar may not be set in all contexts (e.g., startup)
            pass

        # Extract custom fields passed via extra={...}
        extras = {
            k: v
            for k, v in record.__dict__.items()
            if k not in self._STANDARD_ATTRS and not k.startswith("_")
        }
        if extras:
            log_entry["extra"] = extras

        # Include exception traceback if present
        if record.exc_info and record.exc_info[0]:
            log_entry["exc"] = self.formatException(record.exc_info)

        # Serialize to JSON, safely converting non-serializable objects to strings
        return json.dumps(log_entry, default=str)
