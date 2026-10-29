# 汇丰开户客服问答接入记录（2026-10-28）

本模块只回答普通银行账户开户咨询，不提交申请、预约网点或操作银行系统。

## 已接入的问答链路

`hsbc_account` 顶级意图仍由现有 Agent2 规划器路由到 `bank_account`。领域模块现在按以下顺序处理：

1. 用 `account-subintent-v2` 模型提示词识别多标签问题并提取本轮明确条件；模型不可用时使用本地分类规则。
2. 合并当前提问、已确认 Profile 和本轮条件；本轮年龄等明确更正覆盖旧值。`role` 只表示提问者，不能自动当成开户人。
3. 通过 SSH 隧道或生产环境内网连接共享 RDS，读取现有 `ACCOUNT_PRODUCT`、申请渠道、资格规则、材料和费用表，按地区及产品筛选。
4. 程序分别判断产品资格和申请渠道；费用信息单独回答。未知条件标为待确认，渠道不适用不等于产品不适用。
5. 先生成可追溯的基本答案，再由模型按用户具体问法组织语言。模型输出若添加基本答案中没有的数字或改变固定字段，回退到基本答案。
6. 将官网链接、核验日期、候选产品和判断详情写入领域结果，交给原有质量检查与统一回答层。

客服默认使用 RDS 只读仓储。本地测试显式使用只读内存种子，避免自动化测试访问或改动远程数据。2026-10-28 已在原有 RDS 开户目录上补入香港儿童储蓄账户；当时校验行数为 8 个产品、10 条申请渠道、20 条资格规则、22 条材料和 15 条费用记录。后续政策更新需同步 RDS 数据。

本地运行前先建立隧道，再检查数据并启动客服：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start_team03_pg_tunnel.ps1
.\.venv\Scripts\python.exe scripts\sync_hsbc_deposit_accounts.py
.\.venv\Scripts\python.exe -m agent2.cli serve --port 8787 --knowledge fixture
```

## 验证与边界

- `python -m unittest tests.test_bank_account_customer_service tests.test_fx_bank_workflow tests.test_agent2_orchestration tests.test_intent_routing tests.llm.test_ports tests.llm.test_validation -q`：62 项通过。
- `python scripts/smoke_bank_account_answers.py`：用当前配置模型真实试问家长代问、英国学生账户和香港 One 费用；有来源链接。模型写作失败时可回退到数据库生成的基本答案。
- 接入后的 `v2` 用同一套 56 条设计问法复测：结构化输出 56/56，主标签准确率 52/56（92.9%），完整多标签匹配率 41/56（73.2%），标签宏平均 F1 为 92.8%。该语料不是独立真实用户样本；次标签仍会漏，尤其是资格与流程并问。模型可做初步分类，但回答组件会把已知不符合的条件一并展示，避免仅因漏掉资格次标签而丢失关键理由。完整回答质量仍需独立评估。

主要文件：[`bank_account.py`](../agent2/orchestration/domains/bank_account.py)、[`bank_account_repository.py`](../agent2/orchestration/domains/bank_account_repository.py)、[`bank_account_engine.py`](../agent2/orchestration/domains/bank_account_engine.py)、[`bank_account_answer.py`](../agent2/orchestration/domains/bank_account_answer.py)。

## 2026-10-29 香港开户补全与比较修复

复核截图中的“香港开户推荐”及“One 和 Premier 有什么不同”后，确认是数据和 Agent
设计的叠加问题：RDS 虽已有 9 个香港产品，但其中 6 个只有总览页和通用占位规则；同时
旧版产品解析只保留一句话中的一个产品名，因此双产品问题会退化成单产品说明。

本次完成：

- 为全部 9 个香港产品建立逐产品官网存档，并写入
  `oss://ai-campaign-oss-team3/documents/hsbc/`；RDS 有 9 条来源元数据和 36 个检索分片。
- 共享 RDS 经写入和复核后，香港范围共有 9 个产品、14 条渠道、26 条资格规则、
  27 条材料、9 条费用、24 条功能、4 条产品关系。
- 比较请求会保留所有明确出现的产品名；`HSBC One 和 Premier 有什么不同` 固定进入
  `compare_products`，并逐项输出定位、资金/费用条件、渠道及选择建议。
- HSBC One 费用按身份证条件分开表述：香港身份证持有人无低结余费；非香港身份证
  新户适用 10,000 港元总关系结余及 100 港元/月低结余费规则，避免把两条条件并列成
  对所有人都收费。

验证：63 项开户、对话、意图与跨境流程相关测试通过；真实 RDS 问答返回 One 与
Premier 两个候选并产生 5 条官网来源引用。
