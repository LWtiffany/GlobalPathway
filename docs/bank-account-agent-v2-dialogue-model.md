# 留学开户 Agent V2：用户场景与会话任务模型

状态：第一阶段已实现，2026-10-29。适用范围是英、港、美个人银行账户客服；不代用户提交开户申请。

当前实现包括：按会话持久化开户状态、确定性 `dialogue_act`、焦点/排除/选定产品分离、
完整目录与替代候选模式、指代消歧、关键产品依赖说明，以及中文确定性回答回退。
产品资格、渠道、材料和费用继续从已核验数据库读取；大模型只参与细意图理解与受约束的中文改写。

## 为什么当前版本显得“笨”

当前实现解决了顶级路由、单轮细意图、数据库检索和官网引用，但核心对象仍是“这一句话”，而不是“用户正在完成的开户任务”。实际对话显示：

- “别的账户呢，怎么开”曾被归为 `product_selection + documents_process`，但系统没有记录上一轮展示的产品，因此仍重复 Premier Checking。
- “别的户呢”被模型缩成 `documents_process`，系统只返回 Global Money 的流程，没有回答“还有哪些”。
- “只有这俩？”没有基于完整候选集回答，而是把上一轮单个候选误写成当前全部目录。
- “Premier Checking 呢”是对指定产品的聚焦追问，但选择器仍返回两个候选，并提出把“提到的产品”写入 Profile。
- 多轮中 `bank_llm_fallback` 频繁出现，回退模板会机械地重复材料、限制和来源。

因此问题不是再增加一个意图标签，而是缺少以下会话语义：当前焦点、已展示产品、已排除产品、产品依赖、用户决策阶段，以及“提到”和“选定”的区别。

## 用户角色与真实目标

### 角色

1. 学生本人：关注能否开户、何时开、日常消费、收生活费、学费、借记卡和手机银行。
2. 学生家长：替孩子了解开户门槛、未成年人安排、材料、汇生活费和风险；提问者不一定是开户人。
3. 已抵达学生：关注尽快获得本地账户、地址证明、税号、线上失败和替代渠道。
4. 尚未出发学生：关注能否境外预开、抵达后激活、需要提前准备什么。
5. 已有汇丰关系的用户：关注跨境 Premier、现有客户路径、Global Money 或关联储蓄产品。

### 用户最终想完成的任务

用户通常不是来“查一条产品资料”，而是完成下面一条任务链：

```text
要不要开 → 开哪类 → 哪个产品适合 → 我是否符合 → 何时/在哪里开
       → 准备什么 → 费用是否可接受 → 选定产品 → 遇到问题怎么办
```

Agent 必须允许用户从任意节点进入，也允许回退、改条件和换产品。

## 场景目录

| 场景族 | 用户真实问题 | 典型问法 | Agent 应做的事 |
| --- | --- | --- | --- |
| 必要性 | 是否需要当地账户 | “去美国一定要开户吗” | 先回答必要性；区分学费付款与日常银行需求 |
| 开户时机 | 出发前还是抵达后 | “能在国内先开吗” | 比较可用渠道、激活要求及到场条件 |
| 宽泛选品 | 不知道账户类别 | “我要开什么户” | 按日常/储蓄/定期目标分流，不直接抛产品清单 |
| 目标选品 | 已知道用途 | “主要收生活费和刷卡” | 优先日常账户；解释为什么，不推荐定期存款 |
| 储蓄选择 | 想存闲置资金 | “美国有什么储蓄账户” | 只检索 savings，展示关系依赖和流动性 |
| 定期选择 | 想锁定期限收益 | “CD 有哪些” | 只检索 CD/定期，说明期限、最低存款和提前支取 |
| 指定产品 | 追问某一产品 | “Premier Checking 呢” | 聚焦一个产品，不再附带第二候选 |
| 替代产品 | 不喜欢刚才产品 | “别的呢”“换一个” | 排除已展示/当前焦点，再找真正可替代产品 |
| 完整目录 | 质疑候选数量 | “只有这俩？” | 回答目录总数与类别；解释当前只展示两个的原因 |
| 产品比较 | 在两个产品间选 | “One 和 Premier 哪个合适” | 仅比较指定产品的资格、费用、用途和渠道 |
| 资格判断 | 能否申请 | “刚到英国能开学生账户吗” | 三值规则判断；未知时只问一个决定性问题 |
| 未成年人 | 孩子开户 | “17岁去香港怎么开” | 明确实际开户人；比较 Teens/儿童储蓄/成年账户 |
| 材料 | 准备哪些文件 | “要带什么” | 使用当前焦点产品；没有焦点时先明确产品类别 |
| 流程 | 怎么申请 | “App 怎么开” | 给出适用渠道的步骤；先检查渠道前提 |
| 费用 | 月费和余额 | “最低要放多少钱” | 区分开户存款、关系余额和免月费门槛 |
| 产品依赖 | 附属账户前提 | “Global Money 能单独开吗” | 使用产品依赖图；明确不能把附属产品当独立替代品 |
| 条件更正 | 用户改事实 | “刚才说错了，我已经18岁” | 覆盖本轮事实、重新计算、说明结论为何变化 |
| 申请故障 | 已在办理中 | “识别护照失败” | 保留失败渠道/阶段，给排查和官方替代渠道 |
| 决策确认 | 用户选定产品 | “那就开这个” | 才形成 `selected_product`；可提出 Profile 更新建议 |
| 犹豫/放弃 | 暂不决定 | “我再看看” | 保留候选，不写长期 Profile，不继续追问 |
| 跨模块 | 开户与汇款联动 | “开好后家里怎么汇生活费” | 开户和汇款分别处理，并复用已选账户信息 |

## 对话行为，不再只用主题标签

原来的 `product_selection / eligibility / documents_process` 仍可作为知识主题，但必须增加一层 `dialogue_act`：

```text
discover_options       宽泛找方案
filter_options         按用途/类别筛选
focus_product          点名查看一个产品
compare_products       比较两个或多个指定产品
exclude_product        “别的、换一个、不要这个”
ask_catalog_scope      “只有这些吗”
ask_eligibility        资格追问
ask_process            流程追问
ask_documents          材料追问
ask_fees               费用追问
correct_fact           更正条件
confirm_selection      明确选定
reject_selection       明确拒绝
troubleshoot           申请故障
pause_decision         暂停/犹豫
```

`dialogue_act` 决定怎么改变会话状态；知识主题决定调用哪些数据库工具。比如“别的账户呢，怎么开”是 `exclude_product + ask_process`，必须先排除当前焦点，再对新焦点查询流程。

## 会话任务状态

状态应绑定会话，不直接等同于长期 Profile：

```json
{
  "task": "bank_account",
  "market": "US",
  "applicant": {"identity": "student", "relation_to_user": "self"},
  "goal": "daily_banking",
  "stage": "exploring",
  "candidate_product_ids": ["hsbc_us_premier_checking", "hsbc_us_global_money"],
  "presented_product_ids": ["hsbc_us_premier_checking"],
  "focused_product_id": "hsbc_us_premier_checking",
  "rejected_product_ids": [],
  "selected_product_id": null,
  "unresolved_fields": ["applicant_age", "has_us_premier_relationship"],
  "last_dialogue_act": "ask_process",
  "last_answer_mode": "single_product_process"
}
```

关键约束：

- `focused_product_id` 是当前正在谈的产品，可以由提及或展示产生。
- `selected_product_id` 只有在用户明确说“选这个、就办这个、确认”时才设置。
- `presented_product_ids` 用来回答“别的呢”，`rejected_product_ids` 防止重新推荐被拒绝产品。
- 会话焦点不自动写入长期 Profile；只有明确决策后才生成 Profile 变更建议。
- 条件更正触发候选和资格重算，但不清空用户明确拒绝的产品。

## 产品不是平铺列表，而是关系图

需要在产品数据之上增加关系：

```text
standalone_daily_account      可作为主要日常账户
standalone_savings            可独立开的储蓄账户
requires_product              开立前必须持有另一产品
requires_relationship         依赖 Premier/现有客户关系
bundled_subaccount            综合账户内的子账户
upgrade_from / upgrades_to    年龄或客户层级升级关系
```

例子：

- `US Global Money Account -> requires_product -> Premier Checking / Premier Savings / Premier Relationship Savings`
- `US Premier Savings -> requires_product -> Premier Checking`
- `HK Teens -> upgrades_to -> HSBC One or Premier at age 18`
- `HK Premier Elite -> requires_relationship -> HK Premier`

没有这层关系时，Global Money 会被错误地当成 Premier Checking 的平级替代品，这正是截图中“看似有候选但不聪明”的原因之一。

## Agent 工具

不建议让模型直接查询任意 SQL。提供窄而确定的只读工具：

1. `search_accounts(market, goal, applicant, exclude_ids, limit)`：按用途和约束找候选。
2. `get_account(account_id)`：读取产品定位、独立性和依赖关系。
3. `compare_accounts(account_ids, dimensions)`：生成结构化差异，不直接写自然语言。
4. `evaluate_account(account_id, facts)`：运行三值资格规则。
5. `get_opening_routes(account_id, facts)`：返回适用/不适用/待确认渠道。
6. `get_documents(account_id, route_id, facts)`：区分必需和可能要求。
7. `get_fees(account_id, facts)`：区分开户存款、月费、关系余额和豁免。
8. `get_catalog_summary(market, category)`：回答“只有这些吗”，返回真实总数和类别。

这是一个有边界的客服 Agent，不需要开放式无限 loop。建议使用一次规划、最多 3 个只读工具调用、一次回答生成的 bounded harness；只有工具返回缺少决定性字段时才追问用户。

## 规则与大模型的边界

### 必须由规则/程序控制

- 状态转移、焦点/排除/选定产品管理。
- 产品依赖图和“能否独立开立”。
- 候选过滤、资格三值判断、费用计算。
- 点名产品只返回该产品；“别的”必须排除已展示产品。
- 只有明确选择才能生成 Profile 更新建议。
- 每轮最多一个决定性追问；引用必须来自实际使用的规则行。
- 工具调用次数、超时、回退和中文输出校验。

### 适合由大模型完成

- 识别 `dialogue_act` 和用户目标。
- 解析“这个、那个、别的、前一个”等指代，但结果必须经状态校验。
- 从自然语言抽取本轮明确事实和更正。
- 将结构化比较与判断组织成自然、简短的中文客服回答。
- 识别用户是在犹豫、拒绝还是已经确认，而不是仅靠关键词写 Profile。

## 回答策略

回答默认只保留四部分，避免每轮重复免责声明和全量材料：

1. **直接结论**：一句话回答用户这一轮真正问的事。
2. **为什么**：最多三条与决策有关的差异或条件。
3. **下一步**：给一个可行动建议。
4. **一个问题**：只有答案会因该字段改变时才问。

例子：

用户：“别的账户呢？”

> 如果你不考虑 Premier Checking，美国汇丰还有储蓄、CD 和 Global Money。Global Money 不能独立替代日常支票账户，因为它需要已有合资格的美国汇丰账户。你是想找另一个日常消费账户，还是储蓄账户？

用户：“Premier Checking 呢？”

> Premier Checking 是美国汇丰目前的主要日常支票账户，但它面向 Premier 关系客户。要判断是否适合你，关键是能否满足余额、每月直接入账、汇丰按揭或私人银行资格中的一项；否则可能产生月费。你更接近哪一种条件？

用户：“那就这个。”

> 好，我把 Premier Checking 作为当前选定方案。下一步先核对申请年龄和所在地，再给你整理准确的开户渠道与材料。

只有最后一句才允许提出 `account_product = HSBC Premier Checking` 的 Profile 变更。

## 推荐执行流

```text
读取 Profile + AccountConversationState
        ↓
模型识别 dialogue_act / goal / turn facts / references
        ↓
状态归约器校验并更新 focus / exclude / select / corrected facts
        ↓
确定性计划器选择 1~3 个账户工具
        ↓
工具返回结构化、带来源的结果
        ↓
回答策略选择 single / compare / catalog / process / troubleshoot 模式
        ↓
模型生成中文答案 → 数字、产品、费用、引用和状态校验 → 持久化新会话状态
```

## 验收场景

至少新增以下连续对话测试，而不是只测单句分类：

1. “我要开什么户” → 展示两个日常候选并设置焦点。
2. “别的呢” → 不得再次出现上一焦点。
3. “只有这俩？” → 返回目录总数与类别，不得声称数据库只有一个。
4. “Premier Checking 呢” → 只回答 Premier Checking。
5. “这个怎么开” → 使用当前焦点，不重新选品。
6. “不要这个” → 加入拒绝集并推荐下一候选。
7. “那就这个” → 设置 selected，才提出 Profile 更新。
8. “刚才说错了，我17岁” → 重算资格并保留对话选择上下文。
9. “Global Money 能单独开吗” → 明确产品依赖。
10. “我说的是储蓄，不是日常消费” → 更新 goal 并重新检索 savings。
11. 家长连续追问时，实际开户人始终是孩子。
12. 模型超时或校验失败时，确定性回答仍遵守焦点、排除和选定状态。

## 实施顺序

1. 增加 `AccountConversationState` 和持久化表，不改长期 Profile。
2. 将细分类扩展为“知识主题 + dialogue_act”，增加指代解析。
3. 增加产品关系表和受控账户工具。
4. 将 `product_order` 替换为目标驱动检索；移除“固定返回前两个”。
5. 将回答器拆为 single/compare/catalog/process/troubleshoot 五种模式。
6. 禁止“仅提及产品”触发 Profile 建议，只允许显式确认。
7. 建立多轮回归集和指标：焦点准确率、排除遵守率、错误 Profile 建议率、任务完成率、回退率。

