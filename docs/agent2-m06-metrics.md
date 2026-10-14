# Agent2 M06 缺失条件判断与追问：测试指标

对应模块设计 11.3.1。本文件只覆盖 M06；M07、M10 的独立指标与端到端延迟（首次状态反馈 P95 ≤ 3 秒、完整回答 ≤ 30 秒）另行验收。

## 指标定义

| 指标 | 计算 | 目标 |
| --- | --- | --- |
| S1 召回率 | 识别出的必需缺失字段数 / 人工标注的必需缺失字段数（只算 `s1_uk_student_visa` 集合） | 100% |
| 额外追问率 | 追问了但不在人工标注 `relevant_fields` 里的字段数 / 全部追问字段数 | 先记基线，试点 ≤ 10% |
| 规则阶段 P95 | `assess()` 中规则判定耗时，不含模型等待 | ≤ 100 ms |
| 错误 | 下列任一种在固定回归集中出现即不通过 | 0 次 |

错误种类（`agent2/eval_m06.py` 的 `ERROR_KINDS`）：

- `too_many_questions`：一轮追问超过两个关键条件。
- `repeated_ask`：已确认字段被重复追问（字段冲突、取值非法的复核追问不算）。
- `guessed_unknown`：Profile 中状态为 unknown 的值被当作已确认事实使用。
- `definitive_list_without_must_have`：国别或签证类别未知时仍给出确定材料清单。
- `unexplained_state`：异常输入、字段冲突、Profile 版本变化没有返回可解释状态或说明。
- `state_mismatch` / `status_mismatch`：M06 判定或回答状态与人工标注不一致。

模型若参与追问建议，耗时记入 `model_ms`，规则耗时只看 `rule_ms`；模型建议的字段要再过条件表，不在表里的会被丢弃并留下 `suggestion_rejected` 说明。

## 固定回归集

案例在 `agent2/fixtures/m06_cases.json`（夹具版本 `m06-cases-v1`，知识夹具 `uk-student-visa-synthetic-v1`，固定时钟 2026-10-14）。每条案例人工标注 `required_missing`（召回率分母）、`relevant_fields`（额外追问率判定）、期望 `state` 与回答状态。

- `s1_uk_student_visa`（15 条）：缺签证类别、国别和类别都缺、Profile 值状态为 unknown、Profile 与对话冲突、取值非法、Profile 版本变化、多轮补全、未满 18 岁触发条件追问、空消息、单个布尔问题的“是/否”回答等。
- `baseline_other_topics`（4 条）：学费汇款、汇丰开户、信用卡、超出范围，只用于记录基线，不计入 S1 召回率。

## 运行

```bash
.venv/bin/python agent2.py eval-m06 --repeat 50
.venv/bin/python -m unittest tests.test_agent2_m06
```

`eval-m06` 把每个案例重复执行 `--repeat` 次计算 P95，报告写到 `data/agent2/eval/m06-<时间>.json`；未达标时退出码为 1。报告中每条记录包含输入夹具版本、输入、预期结果、实际结果、阶段耗时和 `trace_id`。

## 当前结果（2026-10-14，repeat=50）

| 指标 | 结果 |
| --- | --- |
| S1 召回率 | 1.0 |
| 基线召回率（其他主题） | 1.0 |
| 额外追问率 | 0.0（27 个追问字段） |
| 规则阶段 P95 | 0.008 ms（850 个样本） |
| 模型阶段 P95 | 无（S1 未接模型） |
| 错误 | 全部 0 |

这些结果只说明固定回归集中没有出现上述错误，不代表线上不会出现；样本与部署确定后需要重新校准延迟预算。
