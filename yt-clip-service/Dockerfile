FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir flask==3.0.3 gunicorn==23.0.0 requests==2.32.3 \
 && pip install --no-cache-dir --upgrade --pre yt-dlp[default]

WORKDIR /app
COPY app.py /app/app.py

ENV PORT=7860
EXPOSE 7860

CMD ["gunicorn", "-w", "1", "--threads", "8", "-b", "0.0.0.0:7860", "--timeout", "600", "app:app"]
