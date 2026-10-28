#!/usr/bin/env bash
# 回滚到上一个 tag/sha（本地 build 版）。
# 用法：./scripts/rollback.sh <上一个tag或sha>
set -euo pipefail
cd "$(dirname "$0")/.."

TAG="${1:-}"
if [ -z "$TAG" ]; then
  echo "用法: rollback.sh <上一个tag或sha>"
  echo "可用镜像:"
  docker images globalpathway/admin --format '  {{.Tag}}'
  exit 1
fi

export ADMIN_IMAGE="globalpathway/admin:${TAG}"
export USER_IMAGE="globalpathway/user:${TAG}"
echo "[rollback] 回滚到 ${ADMIN_IMAGE} / ${USER_IMAGE} ..."
docker compose -f docker-compose.prod.yml up -d
echo "[rollback] 状态："
docker compose -f docker-compose.prod.yml ps
