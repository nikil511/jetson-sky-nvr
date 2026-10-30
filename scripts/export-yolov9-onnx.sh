#!/usr/bin/env bash
set -euo pipefail

MODEL_SIZE="${1:-s}"
IMG_SIZE="${2:-320}"
OUT_DIR="${3:-$(cd "$(dirname "$0")/.." && pwd)/config/frigate/model_cache}"
mkdir -p "${OUT_DIR}"

if [ -f "${OUT_DIR}/yolov9-${MODEL_SIZE}-${IMG_SIZE}.onnx" ]; then
    echo "Model ${OUT_DIR}/yolov9-${MODEL_SIZE}-${IMG_SIZE}.onnx already exists."
    exit 0
fi

echo "Exporting yolov9-${MODEL_SIZE}-${IMG_SIZE}.onnx on ARM64..."
docker build . --build-arg MODEL_SIZE="${MODEL_SIZE}" --build-arg IMG_SIZE="${IMG_SIZE}" --output "${OUT_DIR}" -f- <<'DOCKERFILE'
FROM python:3.11 AS build
RUN apt-get update && apt-get install --no-install-recommends -y cmake libgl1 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.10.4 /uv /bin/
WORKDIR /yolov9
ADD https://github.com/WongKinYiu/yolov9.git .
RUN uv pip install --system torch torchvision --index-url https://download.pytorch.org/whl/cpu
RUN uv pip install --system -r requirements.txt
RUN uv pip install --system onnx==1.18.0 onnxruntime onnxsim==0.4.* onnxscript
ARG MODEL_SIZE
ARG IMG_SIZE
ADD https://github.com/WongKinYiu/yolov9/releases/download/v0.1/yolov9-${MODEL_SIZE}-converted.pt yolov9-${MODEL_SIZE}.pt
RUN sed -i "s/ckpt = torch.load(attempt_download(w), map_location='cpu')/ckpt = torch.load(attempt_download(w), map_location='cpu', weights_only=False)/g" models/experimental.py
RUN python3 export.py --weights ./yolov9-${MODEL_SIZE}.pt --imgsz ${IMG_SIZE} --simplify --include onnx
FROM scratch
ARG MODEL_SIZE
ARG IMG_SIZE
COPY --from=build /yolov9/yolov9-${MODEL_SIZE}.onnx /yolov9-${MODEL_SIZE}-${IMG_SIZE}.onnx
DOCKERFILE

ls -lh "${OUT_DIR}/yolov9-${MODEL_SIZE}-${IMG_SIZE}.onnx"
