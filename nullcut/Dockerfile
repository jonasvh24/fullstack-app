# nullcut: API, UI and render workers in one image. The UI ships prebuilt inside
# the Python package (src/nullcut/web_dist; rebuild with `npm run build` in web/).
# Ubuntu's FFmpeg ships libass and NVENC (loaded from the NVIDIA driver at run
# time), and the base exists for amd64 and arm64, so the same file builds on a
# workstation and on DGX Spark.

FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    NULLCUT_HOME=/data
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg python3 python3-venv ca-certificates fonts-dejavu-core curl \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ src/
RUN python3 -m venv /venv && /venv/bin/pip install --no-cache-dir .
ENV PATH=/venv/bin:$PATH
VOLUME ["/data"]
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s CMD curl -fsS http://127.0.0.1:8765/api/health || exit 1
CMD ["nullcut", "serve", "--host", "0.0.0.0", "--port", "8765"]
