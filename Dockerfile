# syntax=docker/dockerfile:1
# AI Document Intelligence Studio. Build:  docker build -t adstudio .   Run: see docker-compose.yml / README.

FROM node:20-slim AS web
WORKDIR /src/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build            # vite writes to ../backend/adstudio/static

FROM python:3.11-slim
# OpenCV needs these at import time on slim images. rapidocr depends on the full opencv-python (not the headless
# build), which links libGL and X11/xcb (libgl1 brings in libxcb1).
RUN apt-get update && apt-get install -y --no-install-recommends libglib2.0-0 libgl1 libxcb1 \
    && rm -rf /var/lib/apt/lists/*
COPY backend/ /app/backend/
COPY --from=web /src/backend/adstudio/static /app/backend/adstudio/static
# Editable install keeps the built frontend (static/) next to the package, where the app looks for it.
RUN pip install --no-cache-dir -e /app/backend
RUN useradd --create-home --uid 1000 app && mkdir /data && chown app:app /data
USER app
ENV ADSTUDIO_DATA_DIR=/data \
    ADSTUDIO_HOST=0.0.0.0 \
    ADSTUDIO_PORT=8765 \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/', timeout=4)"
# Refuses to start without ADSTUDIO_ACCESS_TOKEN (24+ characters): the container listens beyond loopback.
CMD ["python", "-m", "adstudio.main", "--no-browser"]
