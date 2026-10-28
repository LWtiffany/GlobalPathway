#!/usr/bin/env bash
# 在 ECS 上执行的原子部署脚本（本地 build 版，不依赖 GHCR）。
# 用法：./scripts/deploy.sh [tag]   # tag 默认取当前 git sha
set -euo pipefail
cd "$(dirname "$0")/.."

TAG="${1:-$(git rev-parse --short HEAD 2>/dev/null || echo manual)}"
export ADMIN_IMAGE="globalpathway/admin:${TAG}"
export USER_IMAGE="globalpathway/user:${TAG}"

echo "[deploy] 构建镜像 ${ADMIN_IMAGE} ..."
docker compose -f docker-compose.prod.yml build

echo "[deploy] 启动服务 ..."
docker compose -f docker-compose.prod.yml up -d

echo "[deploy] 状态："
docker compose -f docker-compose.prod.yml ps

echo "[deploy] 健康检查："
curl -fsS "http://127.0.0.1:8765/healthz" && echo "" && echo "[deploy] OK"
curl -fsS "http://127.0.0.1:8787/healthz" && echo "" && echo "[deploy] user OK"
