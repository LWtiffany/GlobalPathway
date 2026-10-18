# 信用卡推荐系统 v2（完整系统描述）

本文是 HSBC 香港信用卡推荐系统的完整技术描述：数据模型、资格引擎、奖励估算、排序算法、
风险门控与追问流程。简版流程规范见 [credit-card-recommendation-v1.md](credit-card-recommendation-v1.md)；
LLM 边界细节见 [agent2-llm.md](agent2-llm.md)；整体架构见 [agent2.md](agent2.md)。

设计原则：**所有推荐决策由确定性规则代码拥有**（资格、金额、排序、组合），RDS 中每条规则
带官方来源 URL 与核验时间，LLM 只参与开场自由文本理解与最终答案改写，任何失败都回退到
确定性结果。

## 全链路总览

```mermaid
flowchart LR
    A[用户消息] --> B[规则抽取 + 可选 LLM 理解]
    B --> C[资格评估: hard 门槛 + OR 路径]
    C --> D[分层奖励估算]
    D --> E[年费 + 外币成本]
    E --> F[排序: 门槛状态 → 净值 → 并列规则]
    F --> G[组合: 主推 + 补充卡 + 免年费备选]
    G --> H[质量门控 + 诚实披露]
    H --> I[冻结快照 + Profile 投影]
```

代码位置：

| 层 | 文件 |
| --- | --- |
| 追问编排与回答组装 | `agent2/orchestration/domains/credit_card.py` |
| 纯规则引擎（资格 / 估算 / 排序） | `agent2/orchestration/domains/credit_card_engine.py` |
| 数据合约（Pydantic 模型） | `agent2/orchestration/domains/credit_card_models.py` |
| RDS 只读访问 | `agent2/orchestration/domains/credit_card_repository.py` |
| 引擎行为验收测试 | `tests/test_credit_card_engine_stacking.py` |
| 流程 / service 集成测试 | `tests/test_credit_card_workflow.py` |

引擎是纯函数集合（无 I/O、无状态），全部输入来自 `facts` 字典与产品对象，便于离线测试。

## RDS 数据模型

六张表，全部通过 `agent1.storage.read_session()` 只读访问（推荐请求零写入）：

| 表 | 关键列 | 用途 |
| --- | --- | --- |
| `CREDIT_CARD` | `product_status`、`effective_from/to`、`official_url` | 产品主表；只取 ACTIVE 且在生效窗口内的卡 |
| `CARD_ELIGIBILITY_RULE` | `path_id`、`outcome_if_met`、`source_url` | 资格路径（见下节） |
| `CARD_REWARD_RULE` | `cash_equivalent_rate`、`stacking`、`valid_until`、`qualification_type/value`、`spend_cap/cap_period` | 分层奖励规则 |
| `CARD_FEE` | `waiver_condition`、`waiver_min_annual_spend_hkd`、`rate`、`scope_text` | 年费、外币交易费及豁免条件 |
| `CARD_BENEFIT` | `benefit_type`（`dual_currency`、`lounge_access`…） | 结构化权益（双币、休息室、保险等） |
| `CARD_BORROWING_TERM` | `term_type` 六类、`apr`、`total_repayment`、`source_url/effective_date` | 借款成本，逐条带官方来源 |

字段级溯源取代了旧的整卡 `data_status` 标签（迁移 009 已删除该列）：每条奖励、费用、权益、
借款条款各自携带 `source_url` + `last_verified_at`，数据质量按条判断而非按卡推断。
迁移 009–014 分别引入借款条款表、资格路径列、奖励现金等价率、费用/权益来源列、
奖励资格类型和叠加语义。

仓库层同时返回目录统计（`source_total / source_active / source_filtered`），随答案一起输出，
保证回答可以解释"产品库里有多少张、过滤了多少张"。

## 资格评估引擎

### 路径模型

规则按 `path_id` 分组：**同一路径内条件为 AND，不同路径之间为 OR**。特殊路径 `hard`
存放对所有路径都生效的硬门槛（如最低年龄、香港居民资格）：

```text
CARD_ELIGIBILITY_RULE 示例（Visa Signature）
  hard:        minimum_age ≥ 18
  path_income: minimum_income ≥ 240,000  → meets_public_criteria
  path_asset:  minimum_assets ≥ 400,000  → meets_public_criteria
  path_review: asset_proof = true        → needs_bank_assessment
```

评估结果四种状态：

| 状态 | 含义 | 排序权重 |
| --- | --- | --- |
| `eligible` | 至少一条路径完整通过且结果为公开门槛符合 | 3 |
| `needs_bank_assessment` | 通过的是资产证明等替代路径，最终由 HSBC 个案审批 | 2 |
| `unknown` | 有条件既未通过也未失败（区间跨门槛、资料缺失） | 1 |
| `ineligible` | 硬门槛失败，或所有路径至少一条条件明确不满足 | 排除 |

关键语义：

- **区间跨门槛判 unknown 而非失败**。用户只选了收入区间"100k–199k"而某卡门槛是 150k
  时，不下失败结论，缺精确值就标记 unknown 并把精确字段列入 `missing_fields`。
- **通过的路径吸收其余路径的缺失**。收入路径已通过时，资产路径缺失的 `asset_band`
  不会再作为"缺资料"出现在推荐里。
- **`outcome_if_met` 区分公开门槛与个案审批**。资产路径满足的卡标记
  `needs_bank_assessment`，回答文本固定披露"可提交资产证明，最终由 HSBC 个案审批"。

### 规则类型

| rule_type | 对应 Profile 字段 | 说明 |
| --- | --- | --- |
| `minimum_age` | `applicant_age` / `applicant_age_band` | 18 岁门槛用区间即可判定 |
| `minimum_income` | `annual_income_hkd` / `annual_income_band_hkd` | 精确值或区间（区间跨门槛→unknown） |
| `minimum_assets` | `provable_financial_assets_hkd` / `asset_band` | 如 Signature 的 HK$400,000 |
| `asset_proof` | `asset_proof_available` / `asset_band` | 官方口径：最近 3 个月银行月结单 |
| `student_status` | `full_time_student` | 学生卡专用 |
| `institution_status` | `student_institution_eligible` | HSBC 指定院校名单 |
| `banking_relationship` | `hsbc_premier_path` / `premier_relationship` | Premier Mastercard 路径 |
| `relationship_balance` | `hsbc_total_relationship_balance_hkd` / 区间 | Premier TRB 门槛 |
| `residency` | `hk_resident` | 香港居民基本条件 |
| `credit_assessment` | `credit_history_band` | 预留 |

### 学生与资产的门控逻辑

- **学生问题只在收入不足时出现**：仅当收入低于全部非学生卡的最低收入门槛时才问
  "是否全日制学生"，主流申请人永远看不到学生问题。
- **学生身份与收入独立评估**：有兼职收入的全日制学生，普通卡凭收入照常 `eligible`，
  学生卡同时在候选内；系统不会默认"学生 = 没收入"。
- **资产追问按需触发**：仅当收入路径失败、且产品配置了资产规则时，才问能否提供最近
  3 个月银行资产证明及大致区间。公开的明确资产金额只有 Signature 的 40 万（约收入的
  1.67 倍）；其余卡的"等额资产"没有公开依据，统一走个案审批路径而不虚构确定门槛。
- **Premier 单独问路径**：现有 Premier / 可维持 HK$1,000,000 TRB 并先升级 / 不适用
  三选一；年收入不用于推断 Premier 身份。

## 奖励估算（分层叠加模型）

HSBC 的奖励结构是**加法**的：基础率 + 自选类别加速 + 限时推广，每层独立设上限。引擎
按层建模（`_annual_reward_metrics`）：

```text
每个用户场景 s（按选择顺序加权）:
  年度场景消费 = 月消费中点 × 场景权重 × 12
  total 层: 场景专规则（多个取最高），无则回落基础率
  extra 层: 在 total 之上叠加，各自独立上限
  total 层超上限的溢出部分回落按基础率计算
```

**场景权重 = 选择顺序 = 重要程度**：一个场景 100%；两个 60% / 40%；三个 50% / 30% / 20%。
月消费档位取区间中点估算（如 5k_10k → 7,500；20k_plus → 30,000）。

**官方例子（Pulse，月消费 30k，场景=内地消费，手机/二维码支付）**：

```text
基础层    0.4% × 360,000              = 1,440
QR 推广层 2.0% × min(360k, 80k 上限)  = 1,600   （stacking=extra，限时推广）
自选类别层 2.0% × min(360k, 100k 上限) = 2,000   （最红自主奖赏，按身份证每年共享上限）
──────────────────────────────────────────────
年度奖励估算                        = 5,040 HKD
```

规则要素：

| 要素 | 语义 |
| --- | --- |
| `stacking=total` | 该规则就是该类别的完整费率（替代基础率），历史默认值 |
| `stacking=extra` | 在 total/基础层之上的增量层（如 QR 2%），独立上限 |
| `valid_until` | 限时推广的截止时刻；过期后该层不再贡献任何价值（`now` 由时钟注入，可测） |
| `qualification_type=automatic` | 无条件适用 |
| `=selected_category` | "最红自主奖赏"：整卡每年只能选一个类别。若用户场景含内地消费且用手机/二维码支付，类别保留给内地（组合上限口径），否则给第一个可选场景 |
| `=payment_method` | 需要特定支付方式（Pulse 4.4% 组合上限需手机/二维码） |
| `=designated_merchant` | 指定商户消费 |
| `cash_equivalent_rate` | 积分/里程的官方现金等价率；没有等价率的积分只累计单位、不伪装成现金 |

上限年化：`cap_period=month` ×12、`quarter` ×4、`year` ×1。积分/里程只有存在有来源的
现金等价率才折算港币；用户明确偏好 points/miles 时改按单位数量排序，答案文本同时披露
"约值每年 N 飞行里程 / 奖赏积分"。

## 费用与外币成本

**两年观察期年费**（`_estimated_annual_fee`）：

| 豁免条件 | 观察期年费 |
| --- | --- |
| 永久免年费 / amount=0 | 0 |
| 年度消费门槛豁免（如 EveryMile 年消费 HK$80,000 豁免次年年费） | 用户月消费 ×12 ≥ 门槛时为 0 |
| 首两年豁免 | 0 |
| 首年豁免 | amount ÷ 2 |
| 无豁免 | amount |

**豁免期后年费**（`ongoing_annual_fee`，第三年起）单独计算，用于两件事：净值并列时优先
长期便宜的卡；答案诚实披露"豁免期后年费 HK$X/年"。

**外币手续费**只对 `overseas` / `travel` / `mainland_china` 场景计费：双币卡
（`dual_currency` 权益）的人民币/澳门币场景费率为 0（RMB 账户结算豁免），但人民币/
澳门币**以外**的外币仍按卡费率计（如 1%）——费用优势不扩张为"所有境外免费"。

## 排序与组合

排除规则：只有 `ineligible`（硬门槛失败或全部路径失败）才出局；`unknown` 参与排序但
不会凭高奖励压过已确认门槛的卡。

净值：`年度奖励 − 两年观察期年费 − 外币手续费`。

比较器依次比较（`recommend`）：

1. 门槛状态（eligible > needs_bank_assessment > unknown）；
2. 偏好 points/miles 时先比对应单位数量；
3. 净值，容差 = max(|两卡净值|, 100) × 5%，差异在容差内视为并列；
4. 并列时依次比较：豁免期后年费（低者优先）→ 场景权益分（`benefit_fit`，只给命中场景
   的结构化权益记分，上限 8）→ 场景覆盖权重 → 数据完整度（逐条 source 比例）→ 精确
   净值 → card_id（保证全序确定）。

组合输出：**主推 1 张 + 最多 2 张补充卡 + 最多 3 张免年费备选**。补充卡必须覆盖主推
未覆盖的用户场景且已过门槛；备选必须免年费且不与已选卡重复。完整排名、每项金额、门槛
状态、缺失字段全部保留在 `ranked_candidates` / `payload` 中。

## 借款成本与风险门控

- 用户可能分期或保留余额（`repayment_mode != full`）时，**借款成本优先于奖励**：答案
  第一步展示有官方来源的消费 APR、最长免息期、现金分期官方示例（含期数与总还款额）；
  借款资料不完整时直接 `needs_verification`，不给确定推荐。
- `answered` 的数据门槛：全部展示产品有官方 URL、产品 30 天内核验、且每条奖励/费用/
  权益逐条带 source；未逐项核验时降级 `needs_verification` 并附通知。
- 诚实披露清单（固定追加）：限时推广层到期后按基础率计算；多卡组合时"最红自主奖赏"
  首 HK$100,000 加速奖赏按身份证跨卡共享、不能各享一次；仅覆盖 HSBC 香港；迎新礼遇
  属一次性优惠未计入净值；最终审批由发卡机构决定。

## 追问流程与 Profile

追问顺序（每轮最多两问，`missing[:2]`）：

```text
申请地区 →（年龄）→ 收入区间（选项按 RDS 当前产品门槛动态生成）
→ 香港居民资格（产品有 residency 规则时）
→ Premier 路径（产品有 banking_relationship 规则时）
→ 资产区间（收入不足且有资产路径时）
→ 学生身份 + 指定院校（收入低于最低非学生门槛时）
→ 还款方式 + 消费场景（多选，顺序=权重）
→ 内地支付方式（场景含内地时，Pulse 组合上限所需）
→ 月消费区间 + 奖励偏好
```

- 结构化选项 `persist_mode=save_on_select`：点击即确认为 Profile 事实，走 service 层
  校验（字段白名单、值类型、版本冲突 409）。
- 收入/资产/还款/学生等易变字段带 TTL（365 天，Premier 类 180 天），过期读为 `stale`
  必须重问；历史字段版本追加式记录在 `profile_field_events`。
- 追问答案以 `input_mode=structured_selection` 提交：跳过自由文本抽取（规则和 LLM 都
  不再从选项标签里猜事实），避免"境外消费"这类选项文字被误路由成新意图。
- 全部字段先登记在 `agent2/clarify.py` 的注册表（类型、选项、时效、敏感级、领域归属），
  service 层只接受已登记字段——这也是学生链路 bug 的教训（见测试
  `CreditCardStudentServiceIntegrationTests`）。

## LLM 边界（决策仍然全确定性）

| 环节 | LLM 参与 | 失败回退 |
| --- | --- | --- |
| 开场自由文本 | 理解意图与显式事实（置信度 ≥ 0.80 + 原文证据子串 + 字段白名单） | 确定性正则抽取 |
| 追问作答 | 不参与（structured_selection 直达 Profile） | — |
| 推荐/排序/金额 | 不参与 | — |
| 最终答案 | 只改写 summary/steps 表达；材料与限制逐字节锁定、新数字声明直接拒绝 | 确定性模板文本 |

任何超时、JSON/schema 失败、业务门控失败都回退并追加 `llm_fallback` 通知。详见
[agent2-llm.md](agent2-llm.md)。

## 测试与脚本

- `tests/test_credit_card_engine_stacking.py`：20 个验收用例，覆盖分层叠加与独立上限、
  推广过期翻转排序、消费门槛豁免、豁免期后年费并列优选、双币卡人民币豁免/他币计费、
  里程单位披露、学生门控（低收入才问、有收入学生拿普通卡）、多卡共享上限披露、
  2027 年（推广过期后）推荐等。
- `tests/test_credit_card_workflow.py`：流程与 service 集成（含版本冲突、幂等、
  换条件续问、学生 service 链路）。
- `scripts/chat.py`：交互式命令行试用整个 agent（`--llm` 开 LLM 增强）。
- `scripts/demo_credit_card.py`：连真实 RDS 的自动演示（按当前问题自动作答）。
- 数据同步：`scripts/sync_hsbc_credit_card_*.py` 四个脚本把 HSBC 官方来源的借款条款、
  资格路径、费用权益、奖励价值写入 RDS（写操作只发生在同步脚本，推荐链路只读）。

## 已知边界

- 仅覆盖 HSBC 香港；暂不比较其他发卡机构。
- "等额理财替代收入"没有公开数字依据：Signature 40 万是唯一公开资产门槛，其他卡按
  个案审批建模，不虚构确定金额。
- 迎新礼遇未计入年度净值；申请前以官网最新条款为准。
