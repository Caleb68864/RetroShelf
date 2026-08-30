# RetroShelf — Kavita→iBooks bridge for old iPads.
FROM python:3.12-slim

# tzdata for TZ support; no recommends to keep the image small.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

# OCI provenance so GHCR links the published package to its source repo.
LABEL org.opencontainers.image.title="RetroShelf" \
      org.opencontainers.image.description="Kavita→iBooks bridge for very old iPads — a no-JavaScript, server-rendered OPDS portal." \
      org.opencontainers.image.source="https://github.com/Caleb68864/RetroShelf" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_PORT=8099 \
    TZ=America/Chicago

WORKDIR /app

# Install dependencies first so the layer is cached across code changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Create the unprivileged runtime user (static uid 1000) and the volume mount
# points it owns. The image filesystem itself stays read-only at runtime — the
# app only ever writes to the /config and /cache volumes and /tmp. [SS-08]
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin retroshelf \
    && mkdir -p /config /cache \
    && chown 1000:1000 /config /cache

# Application code, copied in already owned by the runtime uid. Nothing writes
# under /app, so root-owned + world-readable would also work; owning it keeps a
# read-only rootfs tidy without a second recursive chown layer.
COPY --chown=1000:1000 app ./app

USER 1000

EXPOSE 8099

# Healthcheck hits the plain-text /health endpoint. [C-1]
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8099/health', timeout=4).read()==b'ok' else 1)"

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8099"]
