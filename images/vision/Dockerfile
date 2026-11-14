# Forgeyard's vision server: the image behind the ready-made workload templates (object detection,
# segmentation, depth), exposed at /x/<name>. One image, the task chosen by its arguments:
#
#   docker build -t forgeyard-vision:<version> images/vision                              # CPU, amd64 + arm64
#   docker build -t forgeyard-vision:<version>-cuda --build-arg VARIANT=cuda images/vision   # NVIDIA, amd64
#   docker run --rm -p 8000:8000 forgeyard-vision:<version> --task detect
#
# The models are fetched at build time at the revisions pinned in models.json (all Apache-2.0), so a
# container never needs the network; the dependencies are pinned with hashes (requirements-*.txt, from
# compile.sh); the base image by digest. The CUDA variant runs torch +cu130: NVIDIA driver 580 or
# newer. Without a usable GPU it runs on the CPU. It runs as an unprivileged user.

FROM python:3.13-slim-trixie@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS base

FROM base AS deps
ARG VARIANT=cpu
COPY requirements-cpu.txt requirements-cuda.txt /tmp/
RUN set -eu; \
    case "$VARIANT" in \
      cpu) index=https://download.pytorch.org/whl/cpu ;; \
      cuda) index=https://download.pytorch.org/whl/cu130 ;; \
      *) echo "VARIANT must be cpu or cuda" >&2; exit 1 ;; \
    esac; \
    python -m venv /opt/vision; \
    /opt/vision/bin/pip install --no-cache-dir --disable-pip-version-check --require-hashes \
      --extra-index-url "$index" -r "/tmp/requirements-$VARIANT.txt"

FROM deps AS models
COPY models.json download.py /src/
RUN /opt/vision/bin/python /src/download.py /models

FROM base
ARG VARIANT=cpu
ARG VERSION=dev
LABEL org.opencontainers.image.title="Forgeyard vision server" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${VERSION}" \
      org.forgeyard.vision.variant="${VARIANT}" \
      org.forgeyard.vision.models="PekingU/rtdetr_v2_r18vd, facebook/sam2.1-hiera-tiny, depth-anything/Depth-Anything-V2-Small-hf (Apache-2.0)"
COPY --from=deps /opt/vision /opt/vision
COPY --from=models /models /models
COPY app.py models.json /app/
RUN useradd --system --uid 10001 --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin vision
USER 10001
ENV PYTHONUNBUFFERED=1 HF_HUB_OFFLINE=1 HF_HOME=/tmp/hf VISION_MODELS=/models
EXPOSE 8000
HEALTHCHECK NONE
ENTRYPOINT ["/opt/vision/bin/python", "/app/app.py"]
CMD ["--task", "detect"]
