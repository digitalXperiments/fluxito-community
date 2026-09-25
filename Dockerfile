# syntax=docker/dockerfile:1.7
#
# Multi-stage build optimized for layer caching and minimal image size.
# Builder stage compiles wheels with build tools, runtime stage includes only
# runtime dependencies. Final image: ~450MB (asyncpg, cryptography, WeasyPrint native deps).
#
# ── Build stage ─────────────────────────────────────────────────────────────
# Pinned to bookworm (Debian 12): Playwright's `install --with-deps` only knows
# Debian 11/12 + Ubuntu package names. The floating python:3.11-slim tag now
# resolves to Debian 13 (trixie), which Playwright doesn't recognise — it falls
# back to Ubuntu package names that don't exist on Debian and the apt step fails.
FROM python:3.11-slim-bookworm AS builder

WORKDIR /build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# Install build dependencies only (removed from runtime stage).
# libpq-dev, libffi-dev, build-essential are the compiler toolchain.
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies to /install prefix (easier to copy).
# COPY requirements.txt first so code changes don't invalidate this layer.
COPY requirements.txt .
RUN pip install --prefix=/install -r requirements.txt


# ── Runtime stage ──────────────────────────────────────────────────────────
# bookworm (Debian 12) — see the builder-stage note: required for Playwright's
# `install --with-deps chromium` to resolve the right apt packages.
FROM python:3.11-slim-bookworm AS runtime

# Create non-root app user early.
RUN groupadd --system --gid 1000 app && \
    useradd --system --uid 1000 --gid app --create-home --shell /bin/bash app

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_ENV=production

# Exact release version, injected by the release workflow (--build-arg APP_VERSION=X.Y.Z).
# Source/dev builds leave it empty and the app falls back to the VERSION track.
ARG APP_VERSION=""
ENV APP_VERSION=${APP_VERSION}

# Runtime dependencies only (no compiler toolchain).
# curl is needed for healthchecks. Chromium's own OS libraries are installed
# below by `playwright install --with-deps`; we add common fonts here so chart
# labels and text render well in the PDF.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    ca-certificates \
    fonts-dejavu-core \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Copy prebuilt Python packages from builder stage (no recompilation needed).
COPY --from=builder /install /usr/local

# Install the Chromium browser (+ its OS dependencies) that Playwright drives
# to render dashboard PDFs. Stored in a shared path readable by the app user
# (PLAYWRIGHT_BROWSERS_PATH). Runs as root so --with-deps can apt-install libs.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN python -m playwright install --with-deps chromium && \
    chmod -R a+rx /ms-playwright && \
    rm -rf /var/lib/apt/lists/*

# Copy application code (tests, .env, docs, etc. excluded via .dockerignore).
COPY --chown=app:app . .

# Ensure start.sh is executable and owned by app user.
RUN chmod +x start.sh && chown -R app:app /app

# Run as non-root user (never root in production).
USER app

EXPOSE 8001

# Health check: curl /api/health. Tolerates 45s startup for migrations.
# Consecutive failures trigger restart per docker-compose restart policy.
HEALTHCHECK --interval=15s --timeout=5s --retries=3 --start-period=45s \
    CMD curl -fsS http://localhost:8001/api/health || exit 1

CMD ["./start.sh"]
