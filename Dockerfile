FROM python:3.12-slim

# FFmpeg/FFprobe + fonte para marca d'água e relógio da câmera simulada
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend backend
COPY database database
COPY frontend frontend
COPY admin admin

RUN useradd --create-home --uid 10001 replay && mkdir -p /app/data && chown replay:replay /app/data
USER replay

ENV RS_DATA_DIR=/app/data \
    PYTHONUNBUFFERED=1
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=4)" || exit 1

CMD ["python", "-m", "uvicorn", "replayshape.main:create_app", "--factory", "--app-dir", "backend", \
     "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
