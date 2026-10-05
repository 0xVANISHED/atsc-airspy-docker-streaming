# Web TV: a page with a player for Tvheadend's channels; ffmpeg transcodes each
# viewer's channel to H.264/AAC (VAAPI on Intel/AMD GPUs, else libx264).
FROM debian:trixie-slim
ARG DEBIAN_FRONTEND=noninteractive
RUN sed -i 's/^Components: main$/Components: main non-free/' /etc/apt/sources.list.d/debian.sources \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 ffmpeg intel-media-va-driver-non-free mesa-va-drivers \
 && rm -rf /var/lib/apt/lists/*
COPY webtv.py /app/webtv.py
ENTRYPOINT ["python3", "/app/webtv.py"]
