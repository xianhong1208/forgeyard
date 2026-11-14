#!/bin/sh
# Pin the vision server's dependencies with hashes, for both image variants:
#   requirements-cpu.txt   torch +cpu for linux/amd64 and linux/arm64
#   requirements-cuda.txt  torch +cu130 (CUDA 13.0; NVIDIA driver 580 or newer) for linux/amd64
set -eu
cd "$(dirname "$0")"
uv pip compile requirements.in --universal --python-version 3.13 --generate-hashes --no-header \
  --index-strategy unsafe-best-match --extra-index-url https://download.pytorch.org/whl/cpu \
  --override overrides-cpu.txt -o requirements-cpu.txt
uv pip compile requirements.in --python-platform x86_64-manylinux_2_28 --python-version 3.13 --generate-hashes --no-header \
  --index-strategy unsafe-best-match --extra-index-url https://download.pytorch.org/whl/cu130 \
  --override overrides-cuda.txt -o requirements-cuda.txt
