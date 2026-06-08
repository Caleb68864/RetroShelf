# RetroShelf — Kavita→iBooks bridge for old iPads.
FROM python:3.12-slim

# tzdata for TZ support; no recommends to keep the image small.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_PORT=8099 \
    TZ=America/Chicago

WORKDIR /app

# Install dependencies first so the layer is cached across code changes.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application code.
COPY app ./app

# Run as a non-root user that owns the mounted volumes (static uid 1000). [SS-08]
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin retroshelf \
    && mkdir -p /config /cache \
    && chown -R 1000:1000 /config /cache /app
USER 1000

EXPOSE 8099

# Healthcheck hits the plain-text /health endpoint. [C-1]
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8099/health', timeout=4).read()==b'ok' else 1)"

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8099"]
