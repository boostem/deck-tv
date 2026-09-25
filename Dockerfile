# Deck TV: Python stdlib + Debian's ffmpeg. Pinned to trixie, not the moving
# `3.12-slim` tag: trixie's ffmpeg 7.1 is what has -readrate_initial_burst.
FROM python:3.12-slim-trixie

# fonts-dejavu-core is only for the drawtext labels on the sample videos;
# tzdata makes TZ in compose.yaml apply to the log timestamps.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core tzdata \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY app/ /app/
COPY web/ /app/web/

CMD ["python3", "/app/tv.py"]
