#!/usr/bin/env bash

# Keep this script on LF line endings because it executes directly in Linux/WSL.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

MULTIRAG_IMAGE="${1:-${MULTIRAG_IMAGE:-datav/multirag:latest}}"
MULTIRAG_DEPS_IMAGE="${MULTIRAG_DEPS_IMAGE:-multirag_deps:uv0.11.27-tika3.2.3-build-only}"
DOCKER_PLATFORM="${DOCKER_PLATFORM:-linux/amd64}"
NEED_MIRROR="${NEED_MIRROR:-1}"
REDIS_BUILD_JOBS="${REDIS_BUILD_JOBS:-2}"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required to run download_deps.py." >&2
  echo "Install the pinned WSL/Linux version with:" >&2
  echo "  python -m pip install -U 'uv==0.11.27' -i https://pypi.tuna.tsinghua.edu.cn/simple" >&2
  exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
  echo "docker is required to build MultiRAG" >&2
  exit 1
fi

for excluded_path in \
  "configs/local.service_conf.yaml" \
  "core/res/deepdoc/" \
  "core/llm/embedding_model/models/" \
  "models/"; do
  if ! awk -v expected="${excluded_path}" \
    '{ sub(/\r$/, ""); if ($0 == expected) found = 1 } END { exit found ? 0 : 1 }' \
    Dockerfile.dockerignore; then
    echo "Dockerfile.dockerignore must exclude ${excluded_path}" >&2
    exit 1
  fi
done

download_args=()
if [[ "${NEED_MIRROR}" == "1" ]]; then
  download_args+=(--china-mirrors)
  export HF_ENDPOINT="${HF_ENDPOINT:-https://hf-mirror.com}"
fi

echo "[1/4] Downloading build-only resources"
uv run --script download_deps.py "${download_args[@]}"

echo "[2/4] Building ${MULTIRAG_DEPS_IMAGE}"
docker build \
  --platform "${DOCKER_PLATFORM}" \
  --build-arg UV_VERSION=0.11.27 \
  -f Dockerfile.deps \
  -t "${MULTIRAG_DEPS_IMAGE}" \
  .

echo "[3/4] Building ${MULTIRAG_IMAGE}"
docker build \
  --platform "${DOCKER_PLATFORM}" \
  --build-arg NEED_MIRROR="${NEED_MIRROR}" \
  --build-arg MULTIRAG_DEPS_IMAGE="${MULTIRAG_DEPS_IMAGE}" \
  --build-arg REDIS_BUILD_JOBS="${REDIS_BUILD_JOBS}" \
  -f Dockerfile \
  -t "${MULTIRAG_IMAGE}" \
  .

echo "[4/4] Verifying uv and the external-model contract"
docker run --rm --entrypoint uv "${MULTIRAG_IMAGE}" --version
docker run --rm --entrypoint bash "${MULTIRAG_IMAGE}" -c '
  test -d /root/.ragdatav
  test -z "$(find /root/.ragdatav -mindepth 1 -print -quit)"
  test ! -e /multirag/configs/local.service_conf.yaml
  test ! -e /multirag/core/llm/embedding_model/models
  test ! -e /huggingface.co
  test ! -e /multirag/huggingface.co
  test ! -e /multirag/models
  test ! -e /multirag/runtime_models
'

image_size="$(docker image inspect "${MULTIRAG_IMAGE}" --format '{{.Size}}')"
echo "Built ${MULTIRAG_IMAGE} (${image_size} bytes); runtime models are external."
