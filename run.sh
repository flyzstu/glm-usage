#!/usr/bin/env bash
# 启动 GLM 用量服务。支持通过环境变量覆盖任何配置，详见 .env.example。
set -euo pipefail

cd "$(dirname "$0")"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

exec python3 -m glm_usage
