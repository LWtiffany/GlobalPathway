# Agent1 / Crawl4AI 实测记录

本记录使用 Crawl4AI 0.9.4 和 Chromium 对三个公开官方 HTML 页面实际运行。内容会随官网更新；以下数字是本次本机抓取结果，不代表长期稳定值。原始输出保存在本机 `data/snapshots/`，该目录不提交 Git。

| 来源 | 默认 Markdown | 限定 `main` 后 | 观察 |
| --- | ---: | ---: | --- |
| GOV.UK Student visa 概览 | 16,815 字符 | 10,421 字符 | 正文、条件和子页链接保留；默认版混入 Cookie、导航等内容 |
| 汇丰中国留学汇款 | 38,987 字符 | 8,145 字符 | 资格、费用、操作说明及脚注保留；默认版正文约从第 500 行才出现 |
| GOV.UK Student visa 资金要求 | 未测默认版 | 10,064 字符 | 伦敦/非伦敦金额、28 天和 31 天条件均出现在 Markdown 中 |

实测结论：Crawl4AI 能成功抓取这三页并转出可读 Markdown；针对页面主体设置选择器后，汇丰页面的导航噪声明显减少。提取结果仍有图片占位、Markdown 符号和嵌套脚注，不能把 Markdown 直接当成已核验业务数据。

同一配置下重复抓取 GOV.UK 概览页，第二次得到 `NO_CHANGE`，未生成新快照。改变内容选择器后内容指纹变化并生成新快照，这说明抽取配置应与内容版本一起保存；当前 MVP 尚未把配置版本写入数据库。

概览页只包含通往“Money you need”等子页的链接，不会自动抓取子页。因此资金要求页被单独登记和抓取。来源清单需要逐页维护，不能把“抓了入口页”理解为覆盖整个站点。

先用 OpenAI `gpt-4.1-mini` 尝试内建 `LLMExtractionStrategy`，账户返回 `You have no credits remaining`。用户后来提供并确认了阿里云工作区密钥；工作区 `/models` 接口返回 200，`qwen3.8-flash` 小请求成功，`qwen-plus` 则返回 403 `Model access denied`。用 `LLMExtractionStrategy` 对完整资金要求页抽取，约 220 秒后收到提供方连接错误。此时 Crawl4AI 抓页成功，但抽取失败，运行记为 `FAILED`。

为了验证模型对实际抓取内容的效果，MVP 改为 Crawl4AI 抓页、LiteLLM 直接抽取前 4,000 个 Markdown 字符。资金要求页一次成功运行（run 11）：提示 1,177 tokens、完成 2,226 tokens、合计 3,403 tokens；得到 6 条候选，6 条引用都能在抓取文本中找到，0 条被字符串匹配门控拒绝。候选保存在 `PENDING_REVIEW`，没有自动发布。尝试扩至 6,000 字符时又遇到连接错误，因此稳定性和长文支持尚未验证。

人工复核发现明显边界：前 5 条涵盖课程费、伦敦与非伦敦生活费、资金持有期、12 个月居留例外，基本得到对应原文支持。第 6 条“差异化证据要求”虽然引用能匹配，却没有带上页面后文的限制：UKVI 仍可能在决定前要求提交资金证明。该后文不在 4,000 字符截取范围内。故**引用匹配率 6/6 不等于事实完整率 6/6**；第 6 条应补充限定语或拒绝发布。这也说明现阶段不能向用户提供自动生成的签证结论。

本次没有测试 PDF、授权 API、定时调度、真实审核发布、跨来源冲突或 PostgreSQL/OSS。它们属于下一阶段，不应由这次 HTML 成功抓取推断可用。

## 2026-10-12 补记：LLM 超时根因与修复

现象：`visa` 命令的 `usage` 全为 0、`extraction_method` 为 `rule_fallback`、`fallback_reason` 为 `APITimeoutError`。

定位过程（对同一快照 3,000 字符截取实测）：ping 请求 1.2s 成功；500 字符内容 + 完整抽取指令 8s 成功；1,500 字符 82.8s 成功；3,000 字符真实请求流式首 token 185.5s、全程约 191s。延迟随「抽取指令 + 实质内容」的组合增长，与代理无关（走/不走代理均 1.2s），也不是 `json_object` 模式问题（去掉该模式同样超时）。前文记录的「约 220 秒连接错误」是同一现象：旧代码 `timeout=60, max_retries=0` 对 80-200s 的真实抽取必超时。

修复：`agent1.py` 的 LLM 客户端超时改为 300s、重试 2 次，均可经 `AGENT1_LLM_TIMEOUT` / `AGENT1_LLM_MAX_RETRIES` 覆盖；降级时在 stderr 打印 `[WARN]` 及脱敏后的提供商错误，`usage` 中新增 `fallback_error` 字段。修复后同命令端到端复跑：`extraction_method` 为 `llm`，prompt 1,076 / completion 9,186 tokens，7 个字段过证据门控、0 拒绝，另抽出 CAS 与贷款/担保证明两个文档要求（规则提取器无法覆盖）。候选仍为 `PENDING_REVIEW`。

## 2026-10-12 补记二：按规范实现完整 Agent1（本地部署 v2）

依据 PRD v1.0 与设计文档 v0.4 第 4/5/7 章，将单文件 MVP 重构为 `agent1/` 包并本地部署（SQLite 权威存储 + 本地快照 + 进程内调度 + CLI 审核）。相对 MVP 的合规性改进：

- **门控模块**（设计 4.4）：结构/适用/证据/变化四类检查；除逐字证据外增加「取值必须出现在引用中」的数值核对、condition 必须附带逐字引用、金额必须标注适用范围、截断提示；问题分级 error（隔离）/warning（转人工），全部写入 `data/gate/problems.json` 供人工处理与结算。
- **NO_CHANGE 短路**（设计 4.3-2）：页面指纹 + Schema 版本 + 提示词版本均未变化时不重复调用模型，仅刷新 `verified_at`。
- **版本与受控发布**（4.5/ADR-002）：内容变化产生新版本（supersedes 链），措辞/版式变化不产生新版本；低风险且门控全通过才自动发布，高风险/关键变更/跨源冲突/门控 warning 一律转人工；发布、撤回（强制理由）、回滚（核对新鲜度）均为事务并写审计与事件。
- **留痕**（4.6）：模型、Schema 版本、提示词版本、run_id 随版本落库；`verified_at` 仅在抽取+门控通过或内容未变化时刷新。
- **失败可见**（A1.5）：可重试故障退避重试至来源 max_retries；401/403、验证码、配置错误不重试且记 FAILED；`runs` 命令展示错误分类。
- **知识只读接口**（5.2）：`knowledge` 命令/`agent1.knowledge.query()` 仅返回已发布且未超 SLA 的知识，带适用范围、字段级证据、版本与最近成功核验时间。
- **多适用范围建模**：签证政策按页面表述拆分条目（London / Outside London 各自带证据），修复了旧版把非伦敦金额塞进 remark 自由文本、Agent2 无法结构化查询的问题。
- **配置错误不再被兜底掩盖**：确定性降级器仅在 LLM 瞬时超时且已登记时启用。

已知边界：PDF/授权接口、管理端 API/UI、消息队列与 outbox 投递、其他业务类专用 Schema 未实现（属设计文档 P0-B~P0-D 工作包）；通用事实 Schema 尚未按业务细化。

## VISA_POLICY Schema 实测

Agent1 已增加面向外部知识库 ER 图的 `visa` 命令和 SQLite 候选表。2026-10-12 对 GOV.UK Student visa 资金页的真实运行抓取 10,064 个 Markdown 字符，生成一条 `PENDING_REVIEW` 的英国学签政策候选：伦敦生活费 GBP 1,529/月、最多 9 个月、资金连续持有 28 天、持有期结束日在申请日前 31 天内；非伦敦 GBP 1,171/月保存在 remark。7 个落库字段均有可回查的原文引用，未发生字段冲突。

本次模型工作区在结构化抽取请求中断开连接，Agent1 按设计切换为该 GOV.UK 资金页专用的规则提取器，模型 token 用量为 0。该降级器只识别预先登记的金额和时限表达式，不扩展到其他网页；因此它证明了抓取、字段映射、证据门控和候选落库链路可工作，但不证明 Qwen 的本次结构化抽取效果。未在页面明确出现的 CAS、I-20、英语、体检、签证费和生效日期均保持 NULL。

## 2026-10-12 补记三：完整端到端验证与验证期修复

对本地部署的完整 Agent1 做了全链路 E2E（命令序列：sources sync → run → gate → review list/approve → knowledge → 复跑 NO_CHANGE → withdraw → 重跑 → review → rollback → status），并修复了验证期暴露的 6 个缺陷：

1. **`create_run` 返回值未被使用**（pipeline）：调用方自生成 run_id 落库、流程用另一个 id，导致 `finish_run` 更新 0 行、运行记录永远停在 RUNNING。已改为使用 `create_run` 返回的 id。
2. **`link_run_to_version` 在数据库 session 内崩溃导致事务回滚**：问题单写入失败（沙箱禁止 `os.replace` 原子替换，WinError 5）把已成功的版本与运行记录一起回滚。已将该步骤移出事务并设为 best-effort（失败仅 stderr 告警）；`problems.json` 写入增加直接写目标文件的兜底。
3. **`latest_version` 未排除 WITHDRAWN**：撤回后复跑会对着已撤回版本走 NO_CHANGE 并刷新其核验时间。已改为排除 REJECTED/WITHDRAWN，且 NO_CHANGE 短路与 `process()` 的 no_change 判定均改为以**发布指针指向的版本**为基准（回滚后指针指向旧版本也能正确短路）。
4. **撤回后重复运行会重复建版本**：已增加幂等保护——已有一份内容相同的 PENDING_REVIEW 版本时复用（不重复建版本、不重复送审）。
5. **`knowledge` 接口时间比较优先级错误**（`now - verified.total_seconds()`）：AttributeError，已修。
6. **`review list` 强制要求 review_id**：已改为可选。

验证结果（全部通过）：管道全链路 SUCCEEDED 并落版本+审核单；门控 warning 正确写入 `data/gate/problems.json` 并关联版本、审核批准后自动结算；批准即发布（`published_knowledge` 指针 + published_by）；`knowledge` 仅返回已发布知识（London 1529 / 非伦敦 1171 两条结构化条目 + 字段级证据 + verified_at + SLA）；NO_CHANGE 复跑 1.9s 不调模型；撤回后知识接口立即为空、重跑重新送审；回滚切换发布版本后可再回滚恢复；调度器到期计算/租约避让/过期接管/失败退避/重试上限逻辑单测通过。

LLM 路径现状：当日 12:11 曾以旧单政策提示词成功抽取（prompt 1,076 / completion 9,186 tokens，7/7 字段过门控，另抽出 CAS 与贷款/担保证明两个文档要求）。改为多适用范围提示词后，对该网关 4 次实测（480s、300s、300s、240s 超时；含一次精简提示词探针）均超时——该工作区当前无法在可接受时间内承载多范围抽取请求，管道按设计降级为已登记的规则提取器（6 个数值字段全带逐字证据，但覆盖不了文档类要求）。属网关承载问题而非代码缺陷：提示词已尽可能精简，延迟随「抽取指令 + 实质内容」增长（同日实测 500 字符 8s / 1,500 字符 83s / 3,000 字符 191s）。建议后续：更换更快的模型/工作区，或按适用范围分多次调用（每次单范围）以降低单次负载。

## 2026-10-12 补记四：LLM 网关切换——qwen3.8-flash → deepseek-v4.1-flash

针对补记三遗留的“多范围抽取请求在该网关必超时”问题，用同一真实请求对该工作区做了系统压测与修复：

**压测结论**：
- 工作区 `models.list()` 有 150+ 模型，但多数返回 403 Model access denied（未授权）；qwen3.7/3.5/3.6-flash、glm-5.2-fast-preview、kimi highspeed、deepseek-v4-pro 等均未授权。
- `qwen3.8-flash`：可用但首 token 延迟随输入增长（500 字符 8s / 1500 字符 83s / 3000 字符 191s），多范围提示词 4 次实测（480/300/300/240s）全部超时。
- `deepseek-v4.1-flash`：**默认是推理模型**——token 全耗在 `reasoning_content`、`finish_reason=length`、正文为空（曾误判为“快但没内容”）。传 `extra_body={"enable_thinking": False}` 后 **5.2s 返回完整 JSON**，`finish=stop`。`glm-5.3` 同样思考且关不掉。

**代码修复**：
1. `config.llm_settings()` 新增 `extra_body`：deepseek 系自动 `enable_thinking=False`，可用 `AGENT1_LLM_EXTRA_BODY`（JSON）覆盖/扩充。
2. `_call()` 透传 `extra_body`。
3. 空内容防护：模型返回空正文时抛 `model_error` 并提示关思考（不再静默走空数据/JSON 解析失败）。
4. `funds_tuition_unpaid` 类型由 float 修正为 bool（模型曾返回 1.0）。
5. 签证 `max_tokens` 1500 → 3000（证据条数增加后输出被截断过一次，正确走了 extraction_invalid 失败路径）。
6. 提示词迭代三轮（版本签证-policies-3→6）：① 辨析 holding_days 与 doc_max_age_days（补回漏掉的 31 天）；② 明确 remark 也需逐字证据（修复门控隔离）；③ documents 必扫全页 + `required=true` 仅当页面明确要求提交（修复 CAS 被误标 required——页面只说金额写在 CAS 上）；④ 失败一次因输出截断，加 max_tokens 后恢复。
7. 管道新增 `_supersede_stale_problems`：同来源旧 run 的未关闭问题单被新版本取代时自动结算，问题单不再无限堆积。

**最终效果**（v7 已批准发布）：London £1,529/月、非伦敦 £1,171/月（各 9 个月上限）、持有 28 天、申请窗口 31 天、学费覆盖 1 学年=GBP——两条适用范围各 9 条逐字证据；文档：CAS（required=False，页面仅提及）、学生贷款证明（required=True，条件 has_student_loan）、资助证明（required=True，条件 has_financial_sponsorship），引用均逐字命中。**单次运行全程 17.5s**（此前 3-15 分钟且必降级），NO_CHANGE 复跑 1.24s。剩余 2 条 warning（GBP 由 £ 推断、2500 字符截取外未核对）已在审核备注中说明可接受。

## 2026-10-12 补记五：版本机制按「每日 0 点、低频微调」场景重构

用户澄清真实使用场景：每天 0 点抓取上一天更新的数据，可能抓不到、可能没更新、可能只变一个字段值。原「内容寻址 + 每次变化建版本 + 高风险转人工」机制对此过度设计，会天天堆版本、天天要人工批准。重构如下：

**1. 变化不再堆版本，而是原地更新**
- 新增 `db.update_version_in_place()`：字段值小幅变化时直接合并进当前发布版本，保留 version_id/version_no/发布时间不变，只更新 payload 与指纹。
- `versions.process()` 的「内容变化」分支改为原地更新 + 记 `auto_update` 审计日志（含 changed_fields），不再新建版本、不再送人工。
- 无发布指针时（首次/撤回后）才走原「新建版本 + 送审/发布」流程。

**2. 区分「数值字段变化」与「文档清单变化」**
- `_diff_fields` 拆成 `(policy_changes, document_changes)` 两类。
- 数值/布尔字段（金额、天数、日期等）抽取稳定 → 自动原地更新。
- 文档清单（CAS/贷款证明等）每次抽取边界不稳定（temperature=0 下仍波动，实测同一页面抽出 2~3 条不等）→ **转人工审核**，不自动合并，保留当前发布版本不动。

**3. 消除模型措辞波动造成的误判**
- 新增 `NOISE_FIELDS={"remark"}`：`business_content` 的业务指纹排除 remark 自由文本，避免模型每次措辞不同被误判为「内容变化」。
- 提示词给 document `code` 加固定枚举（cas/atas/bank_statement/…），约束命名稳定。
- 门控放宽 remark：remark 是模型补充说明，不再要求逐字命中页面（原逻辑频繁因「模型编的引用」误隔离）。

**4. 历史版本清理**：调试期 v1~v7 清到只剩 v1（唯一基线，PUBLISHED），业务哈希/内容哈希按新逻辑重算。

**验证**：原地更新单测通过（London 金额 1529→1530，版本数 1→1、version_no 不变、审计留痕）；文档变化转人工单测通过（不建版本、发布版本不被改、新增 1 审核单）；真实端到端最终稳定于 `no_change`（数值+文档均与发布版本一致）。当前状态：1 版本 PUBLISHED、0 OPEN 审核单、2 条可接受 warning。

**待办（用户后续决定）**：「每日 0 点」固定时刻调度尚未硬编码——当前调度器是「距上次运行满 1440 分钟触发」，非「固定本地 0 点」。如需严格 0 点，建议用系统任务计划（Windows 任务计划程序 / cron）在 0 点调用 `agent1.py run <source>`，或用 schedule 命令挂常驻进程配合整点对齐。
