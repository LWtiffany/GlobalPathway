#!/bin/sh
# 在 ECS 上一次性启用双入口 Nginx 配置。必须由 root 执行。
set -eu

if [ "$(id -u)" -ne 0 ]; then
  echo "请使用 sudo 执行：sudo sh deploy/install-nginx-prod.sh" >&2
  exit 1
fi

SOURCE=/srv/globalpathway/deploy/nginx-prod-https.conf
# 沿用线上已有站点名，避免旧管理员配置与新 default_server 同时启用。
TARGET=/etc/nginx/sites-available/globalpathway-admin
ENABLED=/etc/nginx/sites-enabled/globalpathway-admin

test -f "$SOURCE"
install -m 0644 "$SOURCE" "$TARGET"
ln -sfn "$TARGET" "$ENABLED"
/usr/sbin/nginx -t
/bin/systemctl reload nginx

echo "用户端：https://123.56.13.78/"
echo "管理员端：https://123.56.13.78/admin/"
