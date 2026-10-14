# GlobalPathway Agent1（本地部署完整版）

## 数据库重构进度（2026-10-13）

已实现环境隔离、新数据库结构、按区域增量扫描、人工审核以及签证政策的发布面（迁移 `004_publication`）；
留学购汇（外汇）政策已可抓取入库（`scan-fx`，迁移 `005_fx_evidence`）。
开发库已初始化并抓取 UK / US / CN-HK 学生签证；正式库未初始化，也未迁移旧数据。
下方「日常使用」之后的原使用说明描述的是兼容管道。

- 默认环境为 `development`；可用 `AGENT1_ENV` 或全局 `--env` 选择。
- 新库：`data/<environment>/agent1.sqlite3`；新快照：`data/<environment>/snapshots/`。
- 兼容管道单独使用 `data/development/legacy/`；禁止在 production 运行。
- `test` 使用系统临时目录，每个进程隔离，正常退出自动清理。
- 原 `data/agent1.sqlite3` 及原快照保留原位，代码不自动读取、复制、清理或重新编号。
- `触发测试.bat` 固定选择 development，不能继承 production。
- 字段定义、关系和后续迁移边界见 [正式数据库方案](docs/agent1-database.md)。

以下为实际使用命令；本次仅在临时测试目录执行，无真实网页或模型调用：

```powershell
.\.venv\Scripts\python.exe agent1.py --env development db init
.\.venv\Scripts\python.exe agent1.py --env production db init
.\.venv\Scripts\python.exe agent1.py --env production db status
```

开发环境增量扫描入口（真实网页和模型调用，需由操作者显式执行）：

```powershell
.\.venv\Scripts\python.exe agent1.py --env development db init
.\.venv\Scripts\python.exe agent1.py --env development db sync-sources --file sources.json
.\.venv\Scripts\python.exe agent1.py --env development scan-region all          # UK / US / CN-HK 全部签证来源
.\.venv\Scripts\python.exe agent1.py --env development scan-region UK --full    # 单个区域强制全量重抽
.\.venv\Scripts\python.exe agent1.py --env development scan uk_student_visa_money
.\.venv\Scripts\python.exe agent1.py --env development scan-fx CN               # 中国留学购汇（外汇）来源 -> FX_POLICY
```

macOS / Linux：`./run-agent1.sh`（= `scan-region all`）、`./run-agent1.sh UK --force`、`./run-agent1.sh uk_student_visa_money`、
`./run-agent1.sh scan-fx CN`。

外汇（`schema_type: fx_policy`，外汇局总局/分局与工行、中行、汇丰页面）按国家合并为两行 `FX_POLICY`
（`purpose_code` = tuition / living），材料写入 `POLICY_DOCUMENT`（`policy_domain='fx_policy'`），
逐字证据写入 `FX_EVIDENCE`；跨页面冲突与已有值变化进入 `REVIEW_QUEUE`（`FX_POLICY`）。
外汇知识目前只入工作表，尚未接入发布面与 Agent2。

签证政策按区域合并：同一目的地的所有官方页面（概览、资金、英语、材料、医疗附加费、TB、费用表等，
见 `sources.json`）合并进同一组 `VISA_POLICY` 行（按 国家 + 就读地范围 唯一），字段级门控，
规则详见 [正式数据库方案](docs/agent1-database.md) 的「按区域合并」。
来源可用 `extraction_notes` 给抽取模型补充该页面的口径说明（如通用收费表中哪一项是学生签证费）。
修改合并/门控规则后，`scan-region all --reapply` 不联网、不调用模型，用已存快照与缓存抽取重新入库。

`scan` 只写工作表（`VISA_POLICY` / `POLICY_DOCUMENT` / `POLICY_EVIDENCE`）、审核项与门控问题，不发布到 Agent2。
默认按来源检查周期到期执行；`--full` 强制整页重新抽取。正式环境的扫描入口保持关闭。

发布是人工操作：发布前检查要求每个字段和材料都有能在快照中逐字找到的证据、没有待审核项和门控错误，
未解决的门控警告需逐条核对后用 `--ack-warnings` 确认。通过后封存为不可修改的修订：

```bash
./run-agent1.sh publication preview UK          # 预览将封存的字段、材料与拒绝原因（只读）
./run-agent1.sh publish UK --ack-warnings --reason "核对 GOV.UK 原文"
./run-agent1.sh publication list                # 发布指针、修订号、核验时间
./run-agent1.sh publication read UK             # Agent2 视角：统一信封 + 可用性
./run-agent1.sh publication withdraw <policy_id> --reason "官方页面更正"
```

之后的扫描若确认已发布事实未变，自动刷新核验时间；事实变化时旧修订保持不变，等待重新发布。
Agent2 只通过 `agent1.publication.search / get` 读取已发布修订。

`db status` 为只读操作；未初始化时只报告状态，不创建文件。
`db init` 只接受空库或同环境的新结构库，不能直接升级混合旧库。
开发兼容管道是空的独立库，需要显式 `--env development sources sync` 后才有来源。

## 兼容管道说明（开发环境）

Agent1 是「留学准备助手」的公共知识采集与发布组件，按《系统设计文档 v0.4》第 4 章实现：

```
来源登记 → Crawl4AI 采集（不可变快照）→ LLM 按 Schema 抽取 → 质量门控 → 变化识别 → 受控发布 / 人工审核
```

- **兼容存储**：本地 SQLite（`data/development/legacy/agent1.sqlite3`）；原文快照在同目录下的 `snapshots/`。
- **兼容门控问题清单**：写入 `data/development/legacy/gate/problems.json`；新结构用 `quality_issues` 表。
- **调度**：进程内定时器（`schedule` 命令），SQLite 任务记录 + 租约防重 + 失败退避重试。
- **审核与发布**：CLI 完成（管理端 API/UI 为后续工作）。高风险新增/变更必须人工批准后才发布（ADR-002）。
- 模型只产出候选知识：无发布权限、无数据库写权限、网页内容仅作数据。

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

LLM 接入（阿里云 MaaS 工作区，key 放 `local_secrets.py`，已被 .gitignore）。**推荐 `deepseek-v4.1-flash`**（实测完整抽取 5-25s，且能抽出文档类要求）：

```powershell
$env:AGENT1_LLM_PROVIDER = 'openai/deepseek-v4.1-flash'
$env:AGENT1_LLM_BASE_URL = 'https://llm-tlndh47wd9ustbss.cn-beijing.maas.aliyuncs.com/compatible-mode/v1'
```

可调参数：`AGENT1_LLM_TIMEOUT`（默认 300s）、`AGENT1_LLM_MAX_RETRIES`（默认 2）、`AGENT1_LLM_EXCERPT_CHARS`（默认 2500）、`AGENT1_LLM_EXTRA_BODY`（JSON，模型级额外参数）。

**模型选择经验（2026-10-12 实测）**：

- `deepseek-v4.1-flash`：默认是推理模型，token 全耗在 `reasoning_content`、正文为空（finish_reason=length）。代码对 deepseek 系自动传 `enable_thinking=False`，关闭后 5-25s 返回完整 JSON。换其他推理模型时需自行确认关闭方式。
- `qwen3.8-flash`：可用但极慢——抽取请求首 token 80-190s，480s 超时仍可能失败，只适合没有其他选择时。
- 该工作区多数其他模型返回 403 Model access denied（未授权），可用 `client.models.list()` 查列表。
- GOV.UK 资金页的关键事实集中在前 2500 字节，缩短截取可降低延迟。

## 日常使用

```powershell
# 1. 同步来源登记（sources.json -> source_registry 表）
.\.venv\Scripts\python.exe agent1.py sources sync
.\.venv\Scripts\python.exe agent1.py sources list

# 2. 手动触发完整管道（采集+抽取+门控+版本路由）
.\.venv\Scripts\python.exe agent1.py run uk_student_visa_money

# 2b. 手动触发口子（测试最省事：双击「触发测试.bat」，或带参数）
触发测试.bat                              # 抓默认来源 uk_student_visa_money
触发测试.bat uk_student_visa              # 指定来源
触发测试.bat uk_student_visa_money --force  # 忽略租约强制重抓

# 3. 定时调度（前台进程；--once 只跑一轮到期来源）
#    注：按 check_interval_minutes 间隔到期触发（默认每日）。如需「固定每天 0 点」，
#    推荐用 Windows 任务计划程序在 0 点执行 `agent1.py run <source>` 更精确。
.\.venv\Scripts\python.exe agent1.py schedule
.\.venv\Scripts\python.exe agent1.py schedule --once --source uk_student_visa_money

# 4. 运营总览 / 运行记录
.\.venv\Scripts\python.exe agent1.py status
.\.venv\Scripts\python.exe agent1.py runs --limit 10
```

## 兼容管道质量门控与问题清单（legacy/gate/problems.json）

门控执行四类检查（设计 4.4），问题分级：

| 级别    | 含义                   | 路由                            |
| ------- | ---------------------- | ------------------------------- |
| error   | 结构/证据/适用性硬失败 | 隔离：版本 `REJECTED`，不发布   |
| warning | 需人工判断             | 转 `PENDING_REVIEW`，不自动发布 |

检查项：Schema 与类型/范围/日期格式；每个非空字段必须有逐字证据且**取值必须出现在引用中**；币种 ISO 代码；`document.condition` 必须附带逐字引用；金额必须标注适用范围（伦敦/非伦敦拆分）；正文超出模型截取范围时提示未核对部分。

```powershell
.\.venv\Scripts\python.exe agent1.py gate            # 查看未结算问题
.\.venv\Scripts\python.exe agent1.py gate --all      # 含已结算
.\.venv\Scripts\python.exe agent1.py gate resolve <problem_id>
```

每个条目含：`check / severity / field / value / quote / reason / suggested_action / run_id / version_id / status`。

## 审核与受控发布

```powershell
.\.venv\Scripts\python.exe agent1.py review list
.\.venv\Scripts\python.exe agent1.py review approve <review_id> [--note "..."]
.\.venv\Scripts\python.exe agent1.py review reject <review_id> --note "理由必填"
.\.venv\Scripts\python.exe agent1.py withdraw uk_student_visa_money --reason "发现错误，紧急阻断"
.\.venv\Scripts\python.exe agent1.py rollback uk_student_visa_money --to <version_id> --reason "..."
```

- 低风险且门控全通过的内容变更才会自动发布；其余一律转人工。
- `withdraw` 立即失效并强制理由；`rollback` 核对旧版本新鲜度后才恢复指针（`--force` 可越过）。
- 发布/撤回/回滚/审核全部写入 `audit_log` 与 `knowledge_events`。

## Agent2 只读知识接口

```powershell
.\.venv\Scripts\python.exe agent1.py knowledge [--schema visa_policy] [--source uk_student_visa_money] [--include-stale]
```

仅返回已发布且当前可用的知识：适用范围、字段级证据、`version_id`、`verified_at`（最近成功核验时间）、可用性。超过来源新鲜度 SLA 的数据默认不作为确定事实返回（PRD 7.4）。Python 内调用：`from agent1 import knowledge; knowledge.query(...)`。

## Agent2 S1（英国 Student visa 最小闭环）

设计见 `docs/agent2-module-design.md` 与 `docs/agent2-s1-uk-student-visa.md`，M06 指标见 `docs/agent2-m06-metrics.md`。

```bash
.venv/bin/python agent2.py serve --port 8787      # 本地 HTTP 服务，Demo 登录（AGENT2_DEMO=0 关闭）
.venv/bin/python agent2.py serve --knowledge fixture   # 改用合成知识夹具
.venv/bin/python agent2.py eval-m06 --repeat 50   # M06 固定回归集，报告写入 data/agent2/eval/
```

- M07 默认读 Agent1 已发布政策（`agent2/agent1_read.py` 调 `agent1.publication.search/get`，`--agent1-env` 选环境，默认 development），引用给出官方页面地址；未发布、已撤回、过期（超过来源 SLA）或快照被改动的记录不作确定回答。
- 覆盖英国、美国、香港学生签证材料。Agent1 的适用条件按 `CONDITION_MAP` 映射到 Profile 字段：英国（年龄、资金来源、停留是否满 6 个月、是否居住在 TB 名单国家）、香港（本地保证人是院校还是个人、保证人是否永久性居民、申请人常住地、是否持有香港身份证）；任一条件键映射不了（如美国“面签前须缴签证费”），该材料整条显示为“待核实”，不当作无条件需要。
- `--knowledge fixture` 与 `eval-m06` 使用 `agent2/fixtures/knowledge_uk_student_visa.json` 合成知识夹具（`synthetic=true`，来源地址为 `example.invalid`），回答带【模拟资料】标识。
- 回答生成目前用模板（M09 `template-v1`），没有接 LLM；M06 的模型追问建议是可选钩子，模型给出的字段仍要过条件表复核。
- 前端（`agent2/web/`，原生 HTML/CSS/JS，无构建）由 `serve` 同址托管，浏览器打开 `http://127.0.0.1:8787/` 即可：模拟登录（学生/家长 × 英/港/美预设）→ 上方案表（基本信息 9 格、汇款/开户/签证/信用卡四类建议、用户待办）· 下对话（进度、追问选项、引用抽屉、质量状态、有帮助/信息有误/继续补充）；另有 `#/onboarding` 建档、`#/report/<profile_id>` 打印报告、`#/feedback` 纠错记录。四类建议里目前只有签证有已发布资料，汇款/开户/信用卡显示「待补充」；管理端页面未做。

## 设计要点与已知边界

- **NO_CHANGE 短路**：页面指纹、Schema 版本、提示词版本均未变化时不重复调用模型，仅刷新 `verified_at`（设计 4.3-2）。
- **版本与追溯**：内容变化产生新版本（`supersedes` 指向上版）；仅措辞/版式变化不产生新版本（`change_type=noise`）。模型、Schema、提示词版本随版本留痕（设计 4.6）。
- **失败重试**：可重试故障（网络/模型超时/限流/输出非法）按退避重试至来源 `max_retries`；401/403、验证码、登录墙、配置错误不重试，运行记 `FAILED` 且原因可见（`runs` 命令）。
- **GOV.UK 资金页规则降级器**：仅在 LLM 瞬时超时且已登记该降级器时启用，输出同样过门控；配置错误不会触发降级。
- 快照按规范化 Markdown 取指纹，导航/版式变动可能改变指纹（由字段级 diff 过滤，不产生新版本）。
- **本轮未实现**（设计已列后续工作包）：PDF 与授权接口采集（A1.2）、管理端 API/UI（7.3）、独立消息队列与 outbox 投递、Agent2 详细设计、跨源冲突的审核界面、其他业务类的专用 Schema（汇款/开户/信用卡目前走通用事实 Schema）。
- 历史混合库保留原位；开发兼容管道使用独立 legacy 库；正式新结构见本文开头与数据库方案。

## 目录

```
agent1/            # 兼容管道与新结构的环境、存储入口
  migrations/     # 新结构 SQL 迁移文件
agent1.py          # CLI 入口
agent2/            # Agent2 S1：意图、M06 追问、M07 知识读取、M08 证据、M09 生成、M10 门控、HTTP API
  fixtures/       # 合成知识夹具与 M06 固定案例
  web/            # 用户端前端（serve 同址托管）
agent2.py          # Agent2 CLI 入口（serve / eval-m06）
sources.json       # 来源登记（含权威等级、负责人、检查周期、SLA、Schema 类型）
data/
  agent1.sqlite3   # 历史混合库，保留原位
  development/    # 开发新结构；legacy/ 为独立兼容管道
  production/     # 正式新结构，须显式 db init 后创建
docs/agent1-database.md # 新库表关系、边界与后续接入说明
tests/             # 隔离环境、增量采集与候选比较的离线测试
local_secrets.py   # LLM API key（gitignored）
EVALUATION.md      # 实测记录
```
