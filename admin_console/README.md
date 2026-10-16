# 管理端审核后台（admin_console）

从 Agent1/Agent2 中独立出来的数据审核后台，**自包含、直接连远程 PostgreSQL**，不再依赖
`agent1`/`agent2` 包（审批落库逻辑已一并移植）。

## 架构

```
admin_console/
  config.py        环境变量配置（隧道默认值）
  db.py            psycopg 连接 + 幂等 schema 初始化（admin schema + 域表）
  auth.py          管理员账号认证（PBKDF2-SHA256，admin.admin_user）
  apply_review.py  批准/驳回落库逻辑（VISA / FX / TUITION，已从 SQLite 移植到 PostgreSQL）
  console.py       AdminConsole：登录 / 列表 / 详情 / 批准 / 驳回
  server.py        独立 HTTP 服务（/v1/admin/* API + web/ 静态页面）
  cli.py           命令行入口（serve / admin add|passwd|remove|list）
  local_secrets.py 数据库密码（git-ignored，不提交）
  web/             前端（admin.html / admin.css / admin.js / app.css）
```

## 前置条件：SSH 隧道

远程 PostgreSQL（ECS `123.56.13.78`，只监听 `127.0.0.1:15432`）通过 SSH 隧道访问：

```powershell
# Windows PowerShell（需要 ~/.ssh/team03_ecs_tunnel 私钥）
powershell -ExecutionPolicy Bypass -File scripts/start_team03_pg_tunnel.ps1
```

连通后本机 `127.0.0.1:15432` 即转发到 ECS 上的 PostgreSQL（库 `team03`，用户 `team03`）。

## 使用

```bash
# 1) 创建管理员账号（密码 ≥ 8 位；add/passwd 会交互式两次输入，或 --password-stdin 一行读入）
python -m admin_console admin add alice

# 2) 启动审核后台（默认 http://127.0.0.1:8765）
python -m admin_console serve

# 其它账号管理
python -m admin_console admin list
python -m admin_console admin passwd alice
python -m admin_console admin remove alice
```

浏览器打开 `http://127.0.0.1:8765`，用管理员账号登录即可看到待审核队列，批准或驳回。

## 配置

全部走环境变量（默认值已对准隧道）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `ADMIN_DB_HOST` | `127.0.0.1` | PostgreSQL 主机（经隧道） |
| `ADMIN_DB_PORT` | `15432` | 端口 |
| `ADMIN_DB_NAME` | `team03` | 数据库 |
| `ADMIN_DB_USER` | `team03` | 用户 |
| `ADMIN_DB_PASSWORD` | （见 local_secrets.py） | 密码 |
| `ADMIN_HOST` / `ADMIN_PORT` | `127.0.0.1` / `8765` | HTTP 监听 |

数据库密码存在 git-ignored 的 `admin_console/local_secrets.py`，不会进入仓库。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/admin/login` | `{username, password}` → `{token, admin}` |
| GET | `/v1/admin/reviews?domain=` | 待审核列表 |
| GET | `/v1/admin/reviews/{id}` | 审核详情（含 proposed_value / evidence / current） |
| POST | `/v1/admin/reviews/{id}/approve` | 批准入库 `{note}` |
| POST | `/v1/admin/reviews/{id}/reject` | 驳回（note 必填） |

## 说明 / 边界

- 启动时会幂等创建缺失的表（`admin.review_queue`、`admin.admin_user` 及 `public` 下的域表
  `VISA_POLICY`/`FX_POLICY`/`TUITION`/`SCHOOL`/`PROGRAM`/`DOCUMENT`/`POLICY_DOCUMENT` 和证据表）。
- **当前远程库的 `review_queue` 是空的**：审核项由 Agent1 抓取/抽取流水线在写库时产生，而
  Agent1 的流水线目前仍走本地 SQLite。要让队列有数据，后续还需把 Agent1 流水线的存储层也切到
  这个 PostgreSQL（或把它的 `_enqueue_review` 落到 `admin.review_queue`）。
- 批准支持 `VISA_POLICY` / `FX_POLICY` / `TUITION` 三个域（与 Agent1 一致）；`ACCOUNT_THRESHOLD`/
  `CREDIT_CARD`/`SCHOOL`/`PROGRAM` 暂只支持驳回（前端已按 `applicable` 禁用批准按钮）。
