#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
WORK_DIR="${HOME}/src"
mkdir -p "${WORK_DIR}"
cd "${WORK_DIR}"

echo "=== 1. Clone gonuit/frigate-jetpack5 (v0.17.2-jp5 reference) ==="
if [ ! -d "${WORK_DIR}/frigate-jetpack5" ]; then
    git clone --branch v0.17.2-jp5 --depth 20 https://github.com/gonuit/frigate-jetpack5.git
fi

echo "=== 2. Extract JetPack 5 patch series onto patches/frigate-0.18.2-jp5 ==="
cd "${WORK_DIR}/frigate-jetpack5"
git format-patch -o "${REPO_DIR}/patches/frigate-0.18.2-jp5" 3d4dd3ac..d3d04714

echo "=== 3. Clone upstream blakeblackshear/frigate at v0.18.2 ==="
cd "${WORK_DIR}"
if [ ! -d "${WORK_DIR}/frigate-0.18.2-jp5" ]; then
    git clone --branch v0.18.2 --depth 1 https://github.com/blakeblackshear/frigate.git frigate-0.18.2-jp5
fi

cd "${WORK_DIR}/frigate-0.18.2-jp5"
git checkout -B v0.18.2-jp5

echo "=== 4. Port JetPack 5 files from gonuit/frigate-jetpack5 onto v0.18.2 ==="
cp -a "${WORK_DIR}/frigate-jetpack5/docker/tensorrt/Dockerfile.arm64" docker/tensorrt/Dockerfile.arm64
cp -a "${WORK_DIR}/frigate-jetpack5/docker/tensorrt/detector/build_python_tensorrt.sh" docker/tensorrt/detector/build_python_tensorrt.sh
cp -a "${WORK_DIR}/frigate-jetpack5/docker/tensorrt/requirements-arm64.txt" docker/tensorrt/requirements-arm64.txt
cp -a "${WORK_DIR}/frigate-jetpack5/docker/tensorrt/trt.mk" docker/tensorrt/trt.mk
cp -a "${WORK_DIR}/frigate-jetpack5/docker/main/Dockerfile" docker/main/Dockerfile
cp -a "${WORK_DIR}/frigate-jetpack5/docker/main/install_deps.sh" docker/main/install_deps.sh
cp -a "${WORK_DIR}/frigate-jetpack5/docker/main/requirements-wheels.txt" docker/main/requirements-wheels.txt

# Ensure FP16 TensorRT engine flag is enabled in frigate/detectors/plugins/onnx.py
if ! grep -q "trt_fp16_enable" frigate/detectors/plugins/onnx.py; then
    python3 -c '
p = "frigate/detectors/plugins/onnx.py"
s = open(p).read()
target = "\"trt_engine_cache_path\": model_dir,"
replacement = target + "\n                    \"trt_fp16_enable\": True,"
if target in s:
    open(p, "w").write(s.replace(target, replacement, 1))
'
fi

git diff > "${REPO_DIR}/patches/frigate-0.18.2-jp5/combined-0.18.2-jp5.patch"

echo "=== 5. Build Frigate 0.18.2-tensorrt-jp5 locally via Buildx ==="
make local-trt-jp5
docker tag frigate:latest-tensorrt-jp5 frigate:0.18.2-tensorrt-jp5
docker images | grep -E "frigate|sky-vision"
