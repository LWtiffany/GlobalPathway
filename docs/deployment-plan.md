# GlobalPathway prod 分支部署方案（CI/CD 标准）

> 目标：把 `prod` 分支部署到阿里云 ECS（`123.56.13.78`），符合 CI/CD 标准，可重复、可回滚、密钥不外泄。
> 文档基于 2026-10-16 的实际代码与服务器状态编写。

---

## 1. 现状盘点

### 1.1 代码与仓库

| 项 | 值 |
| --- | --- |
| 代码仓库 | GitHub：`https://github.com/LWtiffany/GlobalPathway.git`（remote 名 `origin`） |
| 生产分支 | `prod`（已含两条提交：`eae4c82` agent1、`5d5f2aa` admin_console） |
| 开发分支 | `dev`（当前为空，指向 first commit） |

### 1.2 部署的两个组件（性质不同，必须分开处理）

| 组件 | 性质 | 入口 | 依赖 | 数据库 |
| --- | --- | --- | --- | --- |
| **admin_console** | 常驻 HTTP 服务（管理员审核后台） | `python -m admin_console serve`（默认 127.0.0.1:8765） | psycopg、标准库 | 远程 PostgreSQL |
| **agent1** | 批处理（抓取→LLM 抽取→审核→发布） | `python agent1.py <cmd>` / `run-agent1.sh` | crawl4ai(+chromium)、litellm、openai、pydantic | 本地 SQLite（⚠️ 见 §8） |

### 1.3 服务器与数据库（已核实）

| 项 | 值 |
| --- | --- |
| ECS 公网 IP | 123.56.13.78（固定带宽 3 Mbps） |
| 私网 IP | 10.0.0.230 |
| 登录 | root@123.56.13.78:22（SSH key `~/.ssh/team03_ecs_tunnel` 已可用） |
| PostgreSQL | 已在 ECS 本机运行，监听 `127.0.0.1:15432`，库 `team03`，用户 `team03` |
| 到期 | 2026-12-12（包年包月 2 个月，不续费） |

**关键点**：PG 就在 ECS 本机，所以 `admin_console` 部署上去后**直接连 `127.0.0.1:15432`，不再需要 SSH 隧道**（隧道只用于本地开发连远程库）。

---

## 2. 远程部署 Best Practice（调研结论）

综合 GitHub 官方文档、阿里云 DevOps 文档及多篇 2026 年 CI/CD 实践，针对「自有 VPS/ECS 的 SSH 部署」的公认最佳实践：

| # | 原则 | 说明 | 反例 |
| --- | --- | --- | --- |
| 1 | **构建一次，到处部署** | CI 产出不可变产物（Docker 镜像 / 归档包），服务器只拉产物，不在服务器上重新构建 | `git pull && restart` |
| 2 | **产物用 git SHA 打 tag** | 每个镜像/归档都带 `:${sha}`，回滚 = 切回上一个 SHA | 只用 `:latest`（无法精确定位回滚点） |
| 3 | **部署专用用户 + 专用 SSH key** | 不用 root；新建 `deploy` 用户，单独 ed25519 key，`authorized_keys` 加 `command=` 强制命令 + `scoped sudo` | 用 root 或共用个人 key |
| 4 | **密钥只进 Secrets，不进仓库** | DB 密码、LLM key、SSH 私钥放 GitHub Secrets / Environments，运行时注入 | 硬编码、写进 workflow YAML |
| 5 | **生产加审批门** | 生产用 GitHub Environments + required reviewers，或 tag 触发 + 手动确认 | 每次 push 直发生产 |
| 6 | **原子发布 + 一键回滚** | 服务器用 `releases/<sha>/` + 软链 `current`，或镜像 tag 切换；保留最近 N 个版本 | 就地覆盖文件，无法回滚 |
| 7 | **部署后健康检查** | 部署完立即 `curl` 健康端点，失败即让流水线标红并回滚 | 部署完不看结果 |
| 8 | **进程守护 + 自愈** | systemd `Restart=always`，崩溃自动拉起 | 前台 `nohup` 裸跑 |
| 9 | **最小权限 / 最小暴露** | 安全组只开必要端口；PG 只监听 127.0.0.1；scoped sudo 只放行 restart 命令 | 全网段放行、DB 监听 0.0.0.0 |
| 10 | **锁依赖 + 锁 action** | 依赖锁版本；第三方 action pin 到 commit SHA；依赖扫描（bandit/pip-audit） | 用浮动版本、`@master` action |

**结论**：GitHub 仓库 → **GitHub Actions 是默认最优选**（免费额度、原生 Secrets/Environments、GHCR 免费私有镜像仓库、无需额外账号）。阿里云 Flow 是阿里云内的替代方案，但你的仓库在 GitHub，跨平台集成的性价比不如直接用 Actions。

---

## 3. 方案选型对比

| 维度 | **方案 A：Docker + GHCR + Actions（推荐）** | 方案 B：裸机 venv + systemd + 原子目录 | 方案 C：阿里云 Flow 主机部署 |
| --- | --- | --- | --- |
| 环境隔离 | ✅ 强（镜像自包含 chromium/依赖） | ⚠️ 依赖 ECS 装 python3.12 + chromium | ✅ 一般 |
| 回滚速度 | ✅ 秒级（切 tag） | ✅ 秒级（切 symlink） | ✅ 平台内回滚 |
| 与 GitHub 集成 | ✅ 原生 | ✅ 原生 | ⚠️ 需跨平台授权 |
| 带宽友好 | ⚠️ 首拉镜像大（chromium ~1GB+），3Mbps 慢 | ✅ 无大镜像 | ✅ |
| 维护成本 | 低（一次写好） | 中 | 低 |
| 是否符合标准 CI/CD | ✅ 完全 | ✅ 大部分 | ✅ |

**推荐：方案 A（Docker + GHCR + GitHub Actions）**，理由：项目依赖 `crawl4ai` 自带 Playwright chromium，用 Docker 把这一坨重依赖固化进镜像，服务器不用手动装浏览器、不用纠结系统 python 版本，可移植性最强，且 tag=SHA 回滚天然成立。

**但要注意带宽**：ECS 是固定 3 Mbps，chromium 相关镜像首次拉取会较慢（数分钟~十几分钟）。若你觉得太重，可退到方案 B（裸机 venv，服务器上装一次 `playwright install chromium` 后即可复用）。

---

## 4. 推荐方案 A 详细设计

### 4.1 分支与触发策略

```
dev 分支  ──(push/PR)──►  CI：lint + test ──► 自动部署到 staging（可选）
prod 分支 ──(push tag v*)──►  CI 通过 ──► 构建镜像 push GHCR ──► [人工审批] ──► 部署生产 ──► 健康检查
```

- `dev`：每次 push/PR 跑 CI（静态检查 + 测试），**不自动部署生产**。
- `prod`：**tag 触发**（`v1.2.3`）或合并到 prod 后走 GitHub Environment 审批，才发生产。
- 生产环境用 **GitHub Environment「production」+ required reviewers**，任何人合并都不能绕过审批。

### 4.2 目标目录结构（服务器端）

```
/srv/globalpathway/
├── docker-compose.prod.yml      # 生产编排（引用具体镜像 tag）
├── .env                         # 生产密钥（DB 密码、LLM key），权限 600，不入库
├── data/                        # agent1 的 SQLite + 快照（挂载卷，持久化）
└── releases/                    # （方案 B 用）原子发布目录
```

### 4.3 仓库新增文件清单

```
├── Dockerfile.admin             # admin_console 镜像（python:3.12-slim + psycopg）
├── Dockerfile.agent1            # agent1 镜像（crawl4ai + chromium）
├── docker-compose.prod.yml
├── .github/workflows/ci.yml             # PR/push：lint + test
├── .github/workflows/deploy-prod.yml    # tag：build → push GHCR → SSH 部署 → smoke
├── scripts/deploy.sh            # 服务器端原子部署脚本
├── scripts/rollback.sh          # 一键回滚
└── requirements.txt             # 补 psycopg（见 §8）
```

### 4.4 流水线设计

**CI（`.github/workflows/ci.yml`）——每次 PR / push dev**

```yaml
on:
  pull_request:
  push:
    branches: [dev]
jobs:
  ci:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -r requirements.txt
      - run: ruff check .            # 静态检查（可选，先补 .ruff 配置）
      - run: bandit -r agent1 admin_console -q   # 安全扫描
      - run: pytest -q               # ⚠️ 需先重建最小测试（tests/ 已删，见 §8）
```

**CD（`.github/workflows/deploy-prod.yml`）——tag 触发，生产带审批**

```yaml
on:
  push:
    tags: ["v*"]
jobs:
  build:
    runs-on: ubuntu-latest
    permissions: { packages: write, contents: read }
    steps:
      - uses: actions/checkout@v4
      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - uses: docker/build-push-action@v5
        with:
          push: true
          tags: |
            ghcr.io/${{ github.repository }}/admin:${{ github.sha }}
            ghcr.io/${{ github.repository }}/agent1:${{ github.sha }}

  deploy:
    needs: build
    runs-on: ubuntu-latest
    environment: production          # ← 触发 required reviewers 审批
    steps:
      - uses: appleboy/ssh-action@v1   # 实际部署
        with:
          host: ${{ secrets.ECS_HOST }}
          username: deploy             # 专用用户，非 root
          key: ${{ secrets.ECS_DEPLOY_KEY }}
          script: |
            cd /srv/globalpathway
            echo "ADMIN_TAG=${{ github.sha }}" >> .env
            docker compose -f docker-compose.prod.yml pull
            docker compose -f docker-compose.prod.yml up -d

  smoke:
    needs: deploy
    runs-on: ubuntu-latest
    steps:
      - run: |
          curl -fsS http://123.56.13.78/healthz || exit 1   # 失败即标红
```

### 4.5 两个 Dockerfile（要点）

```dockerfile
# Dockerfile.admin —— 轻量，只跑审核后台
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir psycopg[binary]
COPY admin_console ./admin_console
ENV ADMIN_HOST=0.0.0.0 ADMIN_PORT=8765
EXPOSE 8765
CMD ["python", "-m", "admin_console", "serve"]
```

```dockerfile
# Dockerfile.agent1 —— 重量级，含 chromium
FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
      wget gnupg ca-certificates fonts-liberation libnss3 libnspr4 \
      libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 libxkbcommon0 \
      libxcomposite1 libxdamage1 libxfixes3 libxrandr2 libgbm1 libasound2 \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN playwright install --with-deps chromium    # crawl4ai 依赖
COPY agent1 ./agent1
COPY agent1.py sources.json ./
CMD ["python", "agent1.py"]                    # 由 cron/scheduler 触发
```

### 4.6 Docker 容器连宿主机 PG 的关键细节

⚠️ **最易踩坑点**：容器内 `127.0.0.1` 是容器自己，不是宿主机。PG 在宿主机 `127.0.0.1:15432`，容器要连它有 3 种做法，**推荐第 1 种**：

1. **`network_mode: host`**（Linux ECS 上最简单）：容器直接共享宿主机网络，`ADMIN_DB_HOST=127.0.0.1` 即可命中宿主机 PG。
2. `extra_hosts: ["host.docker.internal:host-gateway"]` + `ADMIN_DB_HOST=host.docker.internal`。
3. 把 PG 转发服务改为监听 `10.0.0.230:15432`（不推荐，扩大暴露面，违反最小暴露原则）。

```yaml
# docker-compose.prod.yml（片段）
services:
  admin:
    image: ghcr.io/lwtiffany/globalpathway/admin:${ADMIN_TAG}
    network_mode: host          # 直连宿主机 PG
    env_file: .env
    restart: unless-stopped
```

### 4.7 systemd（守护 + 自愈）

若容器由 compose 管理，用 systemd 守护 `docker compose`；若走方案 B，直接守护 gunicorn：

```ini
# /etc/systemd/system/globalpathway-admin.service
[Unit]
Description=GlobalPathway admin console
After=network.target docker.service
Requires=docker.service

[Service]
WorkingDirectory=/srv/globalpathway
ExecStart=/usr/bin/docker compose -f docker-compose.prod.yml up
ExecStop=/usr/bin/docker compose -f docker-compose.prod.yml down
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### 4.8 Nginx（可选但推荐）

`admin_console` 用的是标准库 `ThreadingHTTPServer`，生产建议前加 Nginx：终止 TLS、反代到 `127.0.0.1:8765`、加 `/healthz`、静态缓存与限流。若嫌重，短期可直接用 8765 端口 + 安全组放行，但**审核后台涉及敏感数据，强烈建议上 HTTPS**。

---

## 5. 安全与密钥管理

| 密钥 | 存放位置 | 说明 |
| --- | --- | --- |
| ECS 部署 SSH 私钥 | GitHub Secrets `ECS_DEPLOY_KEY` | 专用 ed25519 key，仅能以 `deploy` 用户登录，`authorized_keys` 用 `command=` 限制 |
| PG 密码 `team03` | 服务器 `.env`（不入库）+ GitHub Secrets（供 CI 需要时） | 已是本地 `local_secrets.py` 且 gitignored |
| 阿里云 MaaS LLM key | 服务器 `.env` / GitHub Secrets `AGENT1_LLM_API_KEY` | 运行时注入环境变量 |
| 管理员登录 | 数据库 `admin.admin_user`（PBKDF2 存 hash） | 不涉及额外密钥 |

**服务器侧硬化（一次性）**：
1. 新建 `deploy` 用户，`usermod -s /bin/bash`，**禁用密码登录**、只留 SSH key；
2. `~deploy/.ssh/authorized_keys` 加 `command="/usr/bin/docker ..."` 强制命令，或 scoped sudo 只放行 `systemctl restart globalpathway-*`；
3. 安全组：22 端口可临时限制来源 IP；80/443 放行；**15432 保持只监听 127.0.0.1**；
4. 生产用 GitHub Environment 的 required reviewers（至少 1 人审批）。

---

## 6. 回滚与可观测

- **回滚**：`docker compose` 下，把 `.env` 里 `ADMIN_TAG`/`AGENT1_TAG` 改回上一个 SHA，`docker compose up -d` 即完成回滚（秒级）。`scripts/rollback.sh` 封装：`rollback.sh <上一个sha>`。
- **健康检查**：Nginx/或直接 `curl /healthz` 作为部署 smoke 步骤，失败即流水线标红。
- **日志**：容器日志集中到 `journald` / `docker logs`，agent1 批处理跑完输出到日志文件。
- **通知**：可选加 Slack/企业微信通知部署成功/失败。

---

## 7. 落地步骤（分阶段）

**阶段 0 — 准备（本地/仓库）**
1. 补 `requirements.txt` 的 `psycopg[binary]>=3.2`（当前缺失，见 §8）。
2. 重建最小 CI 测试（`tests/` 已删）：至少 `test_smoke.py`（模块可导入 + config 可读 + 一条审核 approve/reject 对临时库）。
3. 提交上面两个改动到 `prod`。

**阶段 1 — CI**
4. 加 `.github/workflows/ci.yml`（lint + bandit + pytest），让 PR 有反馈。

**阶段 2 — 服务器硬化（一次性）**
5. 建 `deploy` 用户 + 专用 key，禁用 root 密码登录。
6. 装 Docker + docker compose。
7. 安全组收敛端口。

**阶段 3 — 镜像与 CD**
8. 写两个 Dockerfile + `docker-compose.prod.yml`。
9. 加 `.github/workflows/deploy-prod.yml`，配置 GitHub Environment「production」+ required reviewer。
10. 配置 GitHub Secrets（`ECS_HOST`、`ECS_DEPLOY_KEY`、`.env` 内容）。

**阶段 4 — 首发 + 验证**
11. 打 tag `v0.1.0` 触发流水线，观察构建 → 审批 → 部署 → smoke 全链路。
12. 验证 admin 后台可登录、审核队列可读；回滚演练一次。

---

## 8. 遗留问题与风险（必须处理）

1. **`requirements.txt` 缺 `psycopg`**：`admin_console` 依赖 psycopg 连 PG，但 requirements 里没有。部署到干净环境会 `ModuleNotFoundError`。→ 阶段 0 补上。
2. **`tests/` 已删，CI 无测试可跑**：CI 的 test 阶段目前是空的，`pytest` 会直接失败。→ 需重建最小 smoke 测试，否则「CI 标准」无从谈起。
3. **`admin_console` 用标准库 `ThreadingHTTPServer`**：能跑，但并发/健壮性弱，且无 `/healthz`。→ 生产建议 gunicorn 化（包一层 WSGI）或至少 Nginx 前置 + 加 `/healthz`。
4. **agent1 仍写本地 SQLite**：审核项产生在 agent1 的本地库，而审核后台读的是远程 PG 的 `admin.review_queue`——**当前两者还没打通**（这是功能遗留，不是部署问题，但影响「审核后台有数据可用」）。→ 需把 agent1 的 `_enqueue_review` 也切到远程 PG，部署才有完整闭环。
5. **带宽 3 Mbps**：chromium 相关镜像约 1GB+，首拉慢。→ 可在 ECS 本地 `docker build` 一次（拉基础镜像仍慢）或接受一次慢拉；方案 B 无此问题。
6. **crawl4ai 需要外网访问目标站点**：agent1 抓 GOV.UK/MIT/香港/外汇局，ECS 出网需正常（国内 ECS 访问 `.hk`/`.cn` 站点通常没问题，境外站点可能受网络策略影响，需验证）。
7. **仓库公开/私有未确认**：公开仓库 GitHub Actions 免费；私有有 2000 分钟/月额度，对低频部署足够。

---

## 9. 需要你决策的点

1. **走方案 A（Docker，重但标准）还是方案 B（裸机 venv，轻但依赖 ECS 环境）？** 我倾向 A。
2. **生产触发方式**：tag 触发 vs 合并到 prod 自动（带审批）。推荐 tag + 审批。
3. **是否现在就补 `psycopg`、重建最小测试**（否则 CI 无法真正跑起来）？
4. **agent1 的审核写库是否也要切到远程 PG**（否则审核后台没数据）——这属于功能改造，需要单独一轮。
5. **是否上 Nginx + HTTPS**（审核后台含敏感数据，推荐上；无域名可用自签或 IP 证书）。

> 需要的话，我可以按方案 A 直接把 Dockerfile、compose、两个 workflow、deploy/rollback 脚本都写出来，并补上 `psycopg` 和最小 smoke 测试。
