# 汇丰港英美开户数据与字段缺口（2026-10-28）

本文件最初只整理可追溯的产品资料和 PostgreSQL 迁移。数据文件为
`agent1/migrations/016_hsbc_deposit_accounts.sql` 和
`agent1/migrations/016_hsbc_deposit_accounts_seed.sql`。后续检查发现 RDS 已有字段命名不同且更完整的开户目录，因此没有用 `016` 覆盖旧表；客服已改为直接读取现有 RDS，并通过
`017_hsbc_hk_children_savings_rds.sql` 补入缺少的香港儿童储蓄账户。当前接入情况见
[开户问答实现记录](hsbc-account-customer-service-implementation.md)。

## 现状与数据库缺口

- Agent2 Profile 已有 `role=student|parent`，代表提问者；`applicant_identity` 代表实际开户人。
  家长替学生咨询时不能把两者合并。当前开户流程会在 `applicant_identity` 空缺时将 `role`
  复制过去，后续应允许用户明确指定“替孩子开户”。
- Agent1 当前 PostgreSQL `ACCOUNT_THRESHOLD` 只有银行、产品、国家、最低年龄、最低存款和居留备注，
  `ACCOUNT_DOC` 只有文档关联。它不能记录同一产品不同申请渠道的资格、资金条件与费用豁免路径。
- `001_domain_schema.sql` 曾设计较完整的 SQLite 开户表，但当前 PostgreSQL `storage.py` 中没有这些表，
  Agent2 也只读取 `hsbc_account_knowledge.py` 的静态目录。本次迁移为 PostgreSQL 添加产品、渠道、
  规则、材料和费用表，并在每条记录保留官网 URL 与核验日期。
- 费用必须与申请资格分开：美国 Premier 的 100,000 美元关系余额或每月 5,000 美元直接入账，
  是维持免月费的路径，不应标成初始开户最低存款。

## 已核实产品与关键差异

| 地区 | 产品 | 与留学场景相关的官网信息 |
| --- | --- | --- |
| 英国 | HSBC Bank Account | 18 岁以上，可提供身份证明与地址证明；官网有境外申请路径，无持有账户月费。 |
| 英国 | Student Bank Account | 18 岁以上，过去 3 年居住在英国、海峡群岛或马恩岛，并有合格课程录取证明；线上申请需 16 位 UCAS 状态码，无账户月费。新到英国未满 3 年者，官网指向普通 HSBC Bank Account。 |
| 美国 | Premier Checking | 至少 18 岁，在线申请所在地需在指定地区；SSN 或有效税务证明可用于在线申请。50 美元月费可通过余额、直接入账、合资格按揭或私人银行关系豁免。 |
| 香港 | HSBC One | App 路径依证件、所在地、年龄和是否新客户而异。2026-01-01 起新开户的非香港身份证持有人，若未维持 10,000 港元三个月平均总关系结余，月收 100 港元低结余费；香港身份证持有人无该门槛或费用。 |
| 香港 | HSBC Premier | 官网列出 1,000,000 港元总关系结余及每月 380 港元低结余费。 |
| 香港 | Children Savings Account | 未满 18 岁；11–17 岁须由家长或监护人陪同到网点，11 岁以下可由家长或监护人以本人名义为孩子开户；无最低余额及月费。 |

来源：[英国普通账户](https://www.hsbc.co.uk/international/apply-for-a-uk-account/)、
[英国学生账户](https://www.hsbc.co.uk/current-accounts/products/student/)、
[美国 Premier Checking](https://www.us.hsbc.com/checking-accounts/products/premier/)、
[美国在线开户 FAQ](https://www.us.hsbc.com/account-opening/frequently-asked-questions/)、
[香港 One](https://www.hsbc.com.hk/accounts/products/one/)、
[香港 App 开户](https://www.hsbc.com.hk/ways-to-bank/mobile-apps/banking/account-opening/)、
[香港跨境开户路径](https://www.hsbc.com.hk/international/banking-in-hong-kong/)、
[香港儿童储蓄账户](https://www.hsbc.com.hk/accounts/products/savings/children/)。

## Profile 后续所需字段

现有字段可复用：`role`、`beneficiary`、`destination_country`、`account_region`、
`applicant_identity`、`applicant_age`、`school`、`offer_status` 和 `planned_start_date`。
`applicant_age_band` 仅区分是否满 18 岁，对香港 App 的 75 岁上限和儿童账户分流不够。

新字段应按用户真正问的问题按需采集，建议优先级如下：

| 字段 | 作用 | 适用场景 |
| --- | --- | --- |
| `account_applicant_relation` | 明确开户人是本人还是子女，避免家长 `role` 被误作开户人 | 家长代问 |
| `uk_residency_years` | 区分英国 Student 与普通 Bank Account | 英国学生选产品 |
| `qualifying_course_offer` | 判断是否有合格课程录取证明 | 英国 Student |
| `application_location` | 区分“开户地区”和申请时实际所在地 | 境外/线上申请 |
| `identity_document_type`、`identity_document_validity` | 区分香港 HKID、护照、通行证路径及有效期 | 香港 App/网点 |
| `existing_hsbc_hk_customer` | 新客户 App 路径限制 | 香港 App |
| `tax_certificate_or_ssn` | 美国在线申请的税务资料条件 | 美国开户 |
| `guardian_present` | 未成年人网点路径 | 香港儿童账户 |
| `average_trb_hkd`、`us_relationship_balance_usd`、`monthly_qualifying_direct_deposit_usd` | 估计是否触发月费；需按地区和币种区分 | 香港 One/Premier、美国 Premier |

这些是未来规则判断需要的 Profile 字段建议，**本次未加入 Agent2 字段注册表**。
账户规则表的 `field_code` 因此只是待接入的数据契约，不能直接用于当前 Agent 的自动判断。

## 数据使用注意

- 官网使用 current/checking/integrated/savings 等不同产品分类；中文“储蓄卡”通常指带借记卡的日常账户，
  但不能把美国 Premier Checking 或英国 Student Bank Account 标成储蓄账户。
- 香港 One 的非香港身份证费用有开户日期条件；旧账户和香港身份证持有人应分开判断。
- 香港非香港身份证持有人申请 One 的 App 与网点说明存在渠道细节，数据表保留独立路径；实际申请时以
  官网当前页面及银行渠道确认结果为准。
- 原静态目录中的英国 `International Student Bank Account` 链接本次查询返回 404；未将该产品写入种子数据。
- 数据插入使用 `ON CONFLICT DO NOTHING`，避免覆盖已人工编辑的行。正式部署前应先检查远端现有表和
  数据，再依次运行 schema 与 seed，并校验行数及来源 URL。

## 2026-10-29 香港目录补全

香港汇丰目录现覆盖 9 个在售个人存款/综合账户产品：HSBC One、HSBC Premier、
HSBC Premier Elite、HSBC Teens、Children Savings Account、Hong Kong Dollar
Savings Account、Hong Kong Dollar Current Account、HSBC Time Deposit 和 Basic
Banking Account with Independence。

之前 One、Premier 和儿童储蓄账户资料较细，其余产品多为总览页占位。本次已按各自
官网补齐用途、申请渠道、公开资格、材料、费用/最低金额、功能和产品依赖，并为全部
9 个产品建立 OSS 原文存档及 RDS 检索分片。同步入口为
`scripts/sync_hsbc_official_account_catalog.py --apply` 和
`scripts/seed_account_source_docs.py --apply --upload-oss`（OSS 上传须在 ECS 执行）。
