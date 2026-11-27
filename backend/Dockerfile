# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# Immagine di produzione Virtualis: backend FastAPI + build frontend React.
# Build dalla RADICE del repository:
#   docker build -f backend/Dockerfile -t virtualis-app .
# (oppure `docker compose build app`, che usa lo stesso contesto)
# ---------------------------------------------------------------------------

# --- Stage 1: build del frontend React (Vite) -------------------------------
FROM node:20-alpine AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# --- Stage 2: backend FastAPI ------------------------------------------------
FROM python:3.12-slim-bookworm AS app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Dipendenze di sistema:
# - libgl1 / libglib2.0-0: richieste da OpenCV/onnxruntime (Docling, fastembed)
# - tesseract-ocr (+ lingue ita/eng): OCR delle scansioni (OCR_ENABLED=true)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    tesseract-ocr \
    tesseract-ocr-ita \
    tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

# uv per l'installazione riproducibile delle dipendenze Python
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /uvx /bin/

WORKDIR /app
COPY backend/pyproject.toml backend/uv.lock ./
# --extra gdrive: connettore Google Drive (google-api-python-client, google-auth)
RUN uv sync --frozen --no-dev --no-editable --extra gdrive
COPY backend/app ./app

# Build del frontend servito dal backend come SPA (settings.frontend_dist)
COPY --from=frontend /build/frontend/dist /srv/frontend/dist

ENV PATH="/app/.venv/bin:$PATH" \
    FRONTEND_DIST=/srv/frontend/dist \
    DATA_DIR=/data \
    UPLOAD_DIR=/data/uploads

EXPOSE 8000

# Healthcheck senza dipendenze extra (python:slim non include curl):
# urllib della stdlib su /api/health. Se TLS è attivo (TLS_CERT_FILE
# valorizzato) si usa HTTPS con verifica del certificato disabilitata
# (self-signed). start_period generoso: al primo avvio il preriscaldamento
# scarica/carica i modelli in background
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import os, ssl, urllib.request; scheme = 'https' if os.environ.get('TLS_CERT_FILE') else 'http'; urllib.request.urlopen(f'{scheme}://localhost:8000/api/health', timeout=4, context=ssl._create_unverified_context())" || exit 1

# `python -m app` avvia uvicorn in HTTP o in HTTPS se TLS_CERT_FILE/TLS_KEY_FILE
# sono impostati (certificato generabile con scripts/gen_cert.py)
CMD ["python", "-m", "app"]
