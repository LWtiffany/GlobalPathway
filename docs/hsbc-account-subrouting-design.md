# 汇丰普通账户开户：细路由与判断契约

状态：单轮细路由设计原稿，2026-10-28；多轮任务状态、对话行为和产品关系图的 V2 设计见 [留学开户 Agent V2](bank-account-agent-v2-dialogue-model.md)。当前客服接入情况见 [开户问答实现记录](hsbc-account-customer-service-implementation.md)。覆盖港、英、美的普通银行账户、借记卡及日常收付款账户咨询；只提供客服问答，不代用户提交银行开户申请或操作银行系统。产品和规则数据见 [开户数据与字段缺口](hsbc-account-data-2026-10-28.md)。

## 入口与边界

顶级意图 `hsbc_account` 进入本模块。细路由为**多标签**，一轮可以同时回答“能否开”和“带什么”；`primary_subintent` 决定先回答哪件事。顶级意图仍是复合意图时，例如“家长怎么汇生活费，我在英国怎么开户”，汇款与开户分别处理，不将汇款方式当作开户条件。

“信用卡怎么交学费”归学费付款/汇款；“哪张信用卡适合交学费”归信用卡选择；“交学费用的收款账户怎么开”归开户。仅说“银行卡怎么弄”而上下文不足时，先用友好问句区分借记卡/日常账户与信用卡。

| 细路由 | 用户要解决的问题 | 典型信号 | 首要输出 |
| --- | --- | --- | --- |
| `account_necessity` | 留学是否需要当地账户，何时开、谁来开 | 一定要、现在要不要、出发前还是落地后 | 必要性/时机及替代选择 |
| `product_selection` | 选哪类汇丰账户 | 哪个适合、One 还是 Premier、学生账户还是普通账户 | 候选产品及适用差异 |
| `eligibility` | 人是否满足某产品或渠道的已公布条件 | 能不能开、年龄、居住年限、证件、SSN | 条件判断和待确认项 |
| `documents_process` | 材料、步骤、预约及 App/网点/境外渠道 | 带什么、怎么申请、线上开、预约 | 可行渠道的流程与材料 |
| `fees_balance` | 开户资金、月费、低结余费及豁免 | 最低存款、管理费、余额要求 | 费用口径、触发条件及来源 |
| `application_issue` | 开户中遇到失败、审核或补件 | 识别失败、提交不了、被拒、补资料 | 可操作的排查或官方联系路径 |

优先级不是固定的标签顺序：若用户明确问“能不能开”，先给资格判断；明确问“要带什么”，先给材料；多问题并列时逐项回答。`application_issue` 需要保留错误阶段与渠道，不应自动推断为资格不符。代词和连续追问可沿用当前会话的主题，但改条件后必须重新计算，不能复用上轮判断结论。

## 输入契约

建议的逻辑输入（字段名优先沿用现有 `Profile`）：

```json
{
  "utterance": "我是家长，孩子17岁去香港，能自己在 App 开 HSBC One 吗？",
  "conversation_context": {"last_product": null, "last_region": null},
  "profile_facts": {"role": "parent", "destination_country": "CN-HK"},
  "turn_facts": {"account_applicant_relation": "child", "applicant_age": 17, "account_region": "CN-HK", "account_product": "HSBC One", "requested_channel": "app"}
}
```

- `role` 是**提问者**（学生/家长）；`applicant_identity` 和 `account_applicant_relation` 指**实际开户人及其与提问者的关系**。家长说“我替孩子问”不能把 `parent` 填入开户人身份。若话中“我”与“孩子”都可能开户，先问为谁开户。
- `destination_country` 是留学目的地；`account_region` 是账户所属地区；`application_location` 是提交申请时人在哪里。这三个地点不能互相覆盖。
- `profile_facts` 只采用状态为已确认的值。用户本轮新说的条件先作为 `turn_facts` 覆盖旧值并触发重算，不悄悄写入长期 Profile；冲突时以本轮明确纠正为准，必要时请求确认。
- 所有事实保留 `value`、`source`（profile/turn/context）、`confirmation_status`（confirmed/candidate/unknown）和时间。缺失、明确“没有”、不确定是三个不同状态。不能把未知当否。
- 按问句取字段。基础字段是产品/地区/开户人；选择或资格判断时再取年龄、英国居住年限、录取状态、证件、申请所在地、现有客户身份等；费用问题才取余额或直接入账等资金条件。字段缺口以 [数据文档](hsbc-account-data-2026-10-28.md#profile-后续所需字段) 为准。

## 决策与数据契约

1. 识别细路由、实体及条件更正；先确认是普通账户还是信用卡/汇款，再确定实际开户人。
2. 从产品、申请渠道、资格规则、材料、费用表检索候选；记录每条规则的 `source_url`、`verified_at`、适用产品/渠道和生效条件。当前迁移 `016_hsbc_deposit_accounts` 尚未应用到远端数据库，运行时在接入前不能声称已使用这些表。
3. **规则引擎**逐条以三值逻辑判断 `true/false/unknown`，分别汇总产品资格、渠道可用性、材料及费用。任何必需条件未知，均不能输出“肯定能开”；渠道不可用也不等于产品不合格。
4. 面向用户的状态：`meets_published_criteria`（已提供事实满足已载明条件）、`does_not_meet_published_criteria`（至少一条已载明必需条件不满足）、`needs_input`（关键条件未知）、`requires_bank_confirmation`（官网没有覆盖或资料有冲突）。这些状态不能写成银行最终审批结果。对于可能随时间变化的政策，回答带来源和核验日期。
5. 费用独立计算 `fee_applies`、`fee_waived`、`fee_unknown`，并说明币种、周期、触发门槛和适用开户日期。香港 One 低结余费、美国 Premier 免月费路径不能写成“开户最低存款”。
6. 仅追问**能改变当前结论或渠道**的最少信息，沿用现有每轮最多 1 个追问的约束。例如英国学生账户问过去 3 年居住地；香港 App 开户问证件/所在地中第一个决定渠道的条件；美国线上申请问 SSN 或税务证明。能给出有用的部分答案时，先答再问。

模型负责理解自然语言、提取候选事实、识别多标签和把规则结果解释得通顺；程序负责产品/渠道筛选、条件计算、费用逻辑、来源引用和追问优先级。模型不得凭常识补造汇丰资格或费用，也不得把用户的“不确定”改成肯定答案。

## 输出契约

```json
{
  "primary_subintent": "eligibility",
  "subintents": ["eligibility", "documents_process"],
  "resolved_subject": {"applicant": "child", "account_region": "CN-HK", "product": "HSBC One"},
  "candidate_results": [{
    "account_id": "hsbc_hk_one",
    "product_status": "needs_input",
    "routes": [{"channel": "app", "status": "needs_input", "blocking_or_unknown_fields": ["identity_document_type", "application_location"]}],
    "fees": {"status": "fee_unknown"},
    "evidence": [{"source_url": "https://www.hsbc.com.hk/ways-to-bank/mobile-apps/banking/account-opening/", "verified_at": "2026-10-28"}]
  }],
  "follow_up": {"field": "identity_document_type", "question": "孩子现在持哪种身份证件？这会影响是否能用 App 申请。"},
  "answer_parts": ["已知情况", "能确认的结论", "下一步"]
}
```

这是**逻辑契约**，未落地的 Profile 字段与运行时接口需在实现时对齐。`answer_parts` 是由判断结果生成的用户表述，不承载新的规则。正式结果应能追溯到规则行和官网链接。

## 例子与验收

| 用户问法/新条件 | 应有行为 |
| --- | --- |
| “我是家长，孩子 17 岁去香港，能自己在 App 开户吗？” | 用孩子的年龄判断；核对产品和 App 渠道；不得用家长年龄或 `role=parent` 判断。 |
| “孩子已经 18 岁了，刚才说错了。” | 用 18 岁重算产品及渠道，更新本轮结论；说明变化来自年龄。 |
| “英国 Student Account 要住三年吗？我刚到英国。” | 说明 Student 条件不满足已知居住条件，同时评估普通 Bank Account；不要推断所有英国账户都不能开。 |
| “HSBC One 最低要放一万吗？” | 区分开户存款与特定客户的低结余收费条件；若身份证/开户日期未知，不断言费用。 |
| “护照识别失败，能换网点吗？” | 归 `application_issue` + `documents_process`，核对证件/渠道说明；不判定被拒。 |

细路由验收数据在 [`tests/fixtures/account_subintent_cases.csv`](../tests/fixtures/account_subintent_cases.csv)。验收至少检查：多标签与主标签、事实提取、家长/学生主体、条件更正后的重算、未知与否的区分、产品与渠道分别判断，以及费用口径。当前分类与客服问答已接入，测试结果见 [实现记录](hsbc-account-customer-service-implementation.md)。
