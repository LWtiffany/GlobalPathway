# Profile 确认与多地区银行账户设计

## 1. 设计目标

Profile 只保存以后仍值得复用、并且已经由用户确认的事实。普通问答中的查询范围、模型推断、产品推荐和临时比较条件不能因为“回答成功”就写入 Profile。

银行账户不能再用一个 `account_region` 或 `account_product` 单值表示。一个学生可以同时拥有或计划开立香港、英国、美国的多个账户，每个地区的进度、产品和申请条件都应独立保存。

## 2. 三层数据边界

### 2.1 学生主档案 Profile

保存跨会话、跨产品仍然成立的学生事实：

| 类别 | 建议字段 | 写入条件 | 更新策略 |
| --- | --- | --- | --- |
| 身份 | 服务对象、与提问者关系、出生日期或年龄段 | 用户直接确认 | 长期有效；年龄应由出生日期计算 |
| 留学计划 | 主要目的地、学校、课程、Offer 状态、入学日期 | 用户直接确认或确认导入结果 | Offer 90 天复核；入学计划变化时保留事件历史 |
| 资格事实 | 居住地、是否香港居民、是否持有香港身份证、全日制学生状态 | 针对明确问题回答或确认提案 | 180–365 天复核 |
| 财务快照 | 收入区间、资产区间、关系结余区间、还款方式 | 用户明确回答且再次确认 | 180–365 天过期；不保存不必要的精确金额 |
| 使用偏好 | 常见消费场景、奖励偏好、月消费区间 | 用户主动选择并同意保存 | 默认 180 天；允许“仅本次使用” |

### 2.2 分地区银行关系 BankRelationship

这是 Profile 下的一组子记录，不是单值字段。建议结构：

```text
BankRelationship
  relationship_id
  profile_id
  institution              # 当前可固定 HSBC，但保留扩展能力
  market                   # CN-HK / UK / US / CN
  product_id               # 对应知识库 account_id；未知时可为空
  product_name_snapshot
  holder_relation          # self / child / parent
  status                   # selected / applying / open / blocked / closed
  opened_on                # 可空，不保存账号或完整账户号码
  source_message_id
  confirmed_at
  last_confirmed_at
  version
  created_at / updated_at
```

允许同一 Profile 存在：

```text
CN-HK + HSBC One             + open
UK    + Student Bank Account + applying
US    + Premier Checking     + selected
```

状态语义必须严格区分：

- `selected`：用户明确说“我决定开这个”，还没有申请。
- `applying`：已经开始申请或准备材料。
- `open`：用户明确说已经成功开立。
- `blocked`：申请失败、补件或等待处理。
- `closed`：账户已关闭；保留历史，不参与默认推荐。

“想看看”“推荐一下”“可能会开”只属于会话，不创建关系记录。产品被 Agent 推荐也不代表用户已选择。

### 2.3 会话状态 Session State

只保存当前对话所需的临时信息：查询地区、候选产品、当前聚焦产品、排除项、未解决问题和本轮假设。

当前 `AccountConversationState.market` 切换地区后会重置旧状态，建议改为：

```json
{
  "active_market": "UK",
  "markets": {
    "CN-HK": {
      "focused_product_id": "hsbc_hk_one",
      "presented_product_ids": ["hsbc_hk_one", "hsbc_hk_premier"],
      "selected_product_id": "hsbc_hk_one",
      "stage": "selected"
    },
    "UK": {
      "focused_product_id": "hsbc_uk_student",
      "presented_product_ids": ["hsbc_uk_student"],
      "stage": "exploring"
    }
  }
}
```

查询地区和当前聚焦产品留在这里，不生成 Profile 变更提案。

## 3. 什么时候算“确定，可以写入”

不能只使用模型置信度。一个事实必须同时满足以下条件：

1. **字段可持久化**：在字段策略表中被标记为 Profile 或 BankRelationship，不能是查询范围或对话控制字段。
2. **陈述对象明确**：确认说的是当前学生本人、孩子或家长，不能把提问者自动当作申请人。
3. **语气是事实或决定**：必须是“我是／我有／我已经开了／我决定开”，不能是疑问、假设、引用或否定范围不明的表达。
4. **作用域明确**：涉及银行账户时必须明确地区；涉及产品时必须解析到唯一 `product_id`。
5. **值通过校验**：枚举、日期、金额区间和产品标识均合法。
6. **没有未解决冲突**：与已确认值不一致时先进入 `disputed` 或提案，不能覆盖旧值。
7. **满足字段确认策略**：敏感、财务、文档导入和关系状态变化必须经过确认。

### 3.1 写入决策表

| 输入来源 | 示例 | 行为 |
| --- | --- | --- |
| 单一明确追问后的按钮选择 | 问“您是否为香港居民？”后点击“是” | 直接确认写入；展示可撤销提示 |
| 单一明确追问后的自然语言回答 | 问“实际开户人是谁？”后回答“我本人” | 解析唯一且无冲突时直接确认 |
| 普通问答中主动陈述稳定事实 | “我今年 20 岁，是全日制学生” | 生成合并确认卡；用户确认后写入 |
| 普通问答中的查询条件 | “我想看看香港账户” | 仅会话使用，不写 Profile |
| 系统根据留学目的地推断 | 英国留学，因此暂按英国开户回答 | 仅本轮假设，不生成提案 |
| Agent 推荐产品 | “HSBC One 比较适合您” | 不写入，不代表用户选择 |
| 用户明确作出选择 | “那我就开 HSBC One” | 确认后新增 `CN-HK / HSBC One / selected` |
| 用户报告完成 | “香港 HSBC One 已经开好了” | 确认后将对应关系更新为 `open` |
| 文档识别结果 | 从文件识别到学校、收入或账户 | 生成提案，显示来源；不能静默写入 |
| 与旧值冲突 | Profile 为英国，用户说“其实去美国” | 标记冲突并让用户确认，不直接覆盖 |

### 3.2 不应写入 Profile 的字段

- `account_region`：它是本轮查询地区，不代表已拥有当地账户。
- `application_region`：它是本轮信用卡申请范围，不代表长期事实。
- `account_product`、`card_product_scope`：它们是当前聚焦或比较对象。
- `want_credit_card`：一次对话中的意愿不是稳定偏好。
- 候选列表、推荐顺序、拒绝的产品、当前对话 act、引用和回答状态。
- 仅由模型补出的事实，以及从目的地、年龄或产品规则推导出的事实。

这些字段应从 `PROFILE_FIELDS_BY_DOMAIN` 的持久化候选中移除，改存 Session State 或关系子表。

## 4. 字段策略注册表

不要再使用“有选项或日期就 `save_on_select`”这一条通用规则。每个字段应显式配置：

```text
storage_class       core_profile | preference | relationship | session_only
confirmation_mode   direct_answer | proposal | always_confirm | never
scope               global | study_plan | market | relationship
freshness_days      null | 90 | 180 | 365
sensitivity         standard | personal | financial | sensitive
```

示例：

| 字段 | storage_class | confirmation_mode | scope |
| --- | --- | --- | --- |
| `destination_country` | core_profile | proposal | study_plan |
| `has_hk_identity_card` | core_profile | direct_answer | global |
| `annual_income_band_hkd` | core_profile | always_confirm | global |
| `reward_preference` | preference | direct_answer | global |
| `account_region` | session_only | never | market |
| `account_product` | session_only | never | market |
| 账户选择与状态 | relationship | always_confirm | relationship |

回答的 `status=answered` 只表示问题得到了回答，不表示本轮提到的事实已经确认写入。

## 5. 多地区账户的对话规则

### 5.1 地区解析优先级

1. 本轮明确地区。
2. 当前 `active_market`。
3. 当前只有一个相关的未完成账户关系。
4. 留学目的地只能作为建议，不能静默写入或覆盖。
5. 同时存在多个可能地区时，给出按钮让用户选择。

示例：用户已有香港和英国账户，问“这个账户月费多少”，但当前没有聚焦产品，应询问：

> 您想继续看香港的 HSBC One，还是英国的 Student Bank Account？

### 5.2 跨地区切换

- “英国也想开一个”创建英国会话分支，香港分支继续保留。
- “回到香港那个”恢复香港的聚焦产品、候选和进度。
- “我有哪些账户”聚合展示所有 `open / applying / selected` 关系，并按地区分组。
- “还有别的账户推荐吗”默认只在当前地区找其他产品；若用户说“其他地区呢”，再切换地区范围。

### 5.3 建议与侧栏存储

当前 `advice_current` 以 `(profile_id, topic)` 为主键，会导致不同地区的账户建议互相覆盖。建议增加 `scope_key`：

```text
(profile_id, topic='account', scope_key='CN-HK')
(profile_id, topic='account', scope_key='UK')
(profile_id, topic='account', scope_key='US')
```

前端“银行账户”区域按香港、英国、美国显示标签页或分组；每个地区独立展示推荐、申请状态和下一步。

## 6. 用户界面

### 6.1 普通事实确认

自然语言中识别到可保存事实时，使用一张合并确认卡，不为每个字段弹一张：

> 要把这些信息更新到学生资料吗？
>
> 全日制学生 · 20 岁 · 香港居民
>
> [更新资料] [仅本次使用]

### 6.2 账户关系确认

> 我理解为：您决定申请香港 HSBC One。目前还没有开立成功，对吗？
>
> [记录为准备申请] [已经开好了] [仅本次讨论]

所有直接写入都提供短时“撤销”入口。财务和敏感字段不在聊天正文重复完整数值。

## 7. 建议的数据表

```sql
CREATE TABLE bank_relationships (
    relationship_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL REFERENCES profiles(profile_id),
    institution TEXT NOT NULL,
    market TEXT NOT NULL,
    product_id TEXT,
    product_name_snapshot TEXT NOT NULL,
    holder_relation TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN
        ('selected','applying','open','blocked','closed')),
    opened_on TEXT,
    source_message_id TEXT,
    confirmed_at TEXT NOT NULL,
    last_confirmed_at TEXT NOT NULL,
    version INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE bank_relationship_events (
    event_id TEXT PRIMARY KEY,
    relationship_id TEXT NOT NULL REFERENCES bank_relationships(relationship_id),
    old_status TEXT,
    new_status TEXT NOT NULL,
    source_message_id TEXT,
    actor_type TEXT NOT NULL,
    created_at TEXT NOT NULL
);
```

不要保存完整账号、银行卡号、身份证号码或税号。这里只保存产品关系和申请状态。

## 8. 落地顺序

### 第一阶段：先修正错误写入

1. 增加字段策略注册表。
2. 将 `account_region`、`application_region`、`account_product`、`card_product_scope`、`want_credit_card` 改为 `session_only`。
3. 只有明确追问答案才能直接写入；普通陈述改为合并提案。
4. 冲突字段不得覆盖，必须确认。

### 第二阶段：支持多个账户关系

1. 新增 `bank_relationships` 和事件表。
2. 把账户选择、申请中、已开户、受阻和关闭状态迁移到关系记录。
3. Account Session State 改为按市场保存多个分支。
4. `advice_current` 增加 `scope_key`。

### 第三阶段：改造前端

1. Profile 页面分为“学生资料”“偏好”“银行账户”。
2. 银行账户按地区分组，可新增、更新状态和关闭。
3. 聊天确认卡提供“更新资料／仅本次使用”和撤销能力。

旧的 `account_region`、`application_region` 和 `account_product` 不应自动迁移为“已开户”，因为旧值只能证明曾经讨论或申请过该地区。只有用户重新确认后才能生成关系记录。
