"""Code-owned second-layer capability planning.

The model/rule domain router answers *where* a question belongs.  This planner
answers *what the user wants to do there*.  It emits no persistence policy;
effects remain owned by LifecyclePlanner and the service layer.
"""

from __future__ import annotations

import re

from .contracts import (CapabilityAction, CapabilityOperation, DomainTask,
                        EntityMention, OrchestrationInput, TaskPlan)

CAPABILITY_PLANNER_VERSION = "capabilities-rules-1"

_OPERATION_PATTERNS: tuple[tuple[CapabilityOperation, re.Pattern], ...] = (
    ("correct", re.compile(r"说错了|不是.{0,12}是|改成|更正|刚才.{0,12}错", re.I)),
    ("reject", re.compile(r"不要这个|不考虑这个|排除这个|这个不行|不选这个", re.I)),
    ("request_alternatives", re.compile(r"别的|其他|另外|换一个|下一个|之外", re.I)),
    ("select", re.compile(r"就这个|选这个|办这个|开这个|就它|决定(?:选|办|开)", re.I)),
    ("troubleshoot", re.compile(r"失败|被拒|拒绝|补件|补材料|卡住|报错|办不了|开不了|怎么办", re.I)),
    ("compare", re.compile(r"对比|比较|区别|差别|哪个好|哪一个好|versus|\bvs\.?\b", re.I)),
    ("assess", re.compile(r"适合我|能申请|可以申请|能不能|可不可以|符合|资格|获批|批得下来|要不要|需不需要|是否需要|有必要", re.I)),
    ("how_to", re.compile(r"怎么(?:办|申请|开|汇)|如何|流程|步骤|需要什么|准备什么|材料", re.I)),
    ("recommend", re.compile(
        r"推荐|帮我选|帮我挑|怎么选|选哪|哪张|哪种|最适合|重新生成|重新推荐"
        r"|(?:办|申请|选|挑).{0,6}(?:哪张|哪种|什么)(?:信用)?卡"
        r"|(?:什么|哪款|哪个)(?:信用)?卡.{0,10}(?:适合|合适|值得办)"
        r"|(?:适合|合适).{0,8}(?:信用卡|卡).{0,4}(?:有|吗)"
        r"|(?:信用卡|卡).{0,8}(?:适合我|合适)", re.I)),
    ("explain", re.compile(r"怎么样|好不好|值得|是什么|介绍|讲讲|评价|优缺点|年费|费用|权益|回赠|返现|积分|里程", re.I)),
    ("list_options", re.compile(r"有哪些|有什么|全部|完整列表|一共几个|只有这|只有这些", re.I)),
    ("focus", re.compile(r"这个|这一个|它|刚才那个", re.I)),
)

_ASPECT_PATTERNS = {
    "fees": re.compile(r"年费|费用|手续费|利率|APR|借款成本", re.I),
    "rewards": re.compile(r"权益|回赠|返现|积分|里程|奖赏|优惠", re.I),
    "eligibility": re.compile(r"资格|条件|能申请|可以申请|获批|居民|收入|年龄", re.I),
    "documents": re.compile(r"材料|文件|证明|证件", re.I),
    "process": re.compile(r"流程|步骤|怎么|如何", re.I),
    "timeline": re.compile(r"多久|时间|截止|什么时候", re.I),
    "limits": re.compile(r"额度|限额|上限", re.I),
    "necessity": re.compile(r"要不要|需不需要|是否需要|有必要|必须", re.I),
    "alternatives": re.compile(r"别的|其他|另外|换一个|下一个|之外", re.I),
}

_ENTITY_MENTION = re.compile(
    r"(?:HSBC\s+)?[A-Za-z][A-Za-z0-9+\- ]{0,48}?(?:Credit Card|Card|Account|Checking|Savings)"
    r"|(?:Red|Pulse|EveryMile|Premier|Student)\s*(?:信用卡|卡|账户)?",
    re.I,
)

_DEFAULT_OPERATION: dict[str, CapabilityOperation] = {
    "credit_card": "recommend",
    "bank_account": "how_to",
    "visa": "how_to",
    "fx": "how_to",
}


class RuleCapabilityPlanner:
    version = CAPABILITY_PLANNER_VERSION

    async def enrich(self, plan: TaskPlan, request: OrchestrationInput) -> TaskPlan:
        tasks = [self._enrich_task(task, request.text) for task in plan.tasks]
        return plan.model_copy(update={
            "tasks": tasks,
            "planner_version": f"{plan.planner_version}+{self.version}",
        })

    def _enrich_task(self, task: DomainTask, text: str) -> DomainTask:
        mentions = [EntityMention(text=match.group(0).strip())
                    for match in _ENTITY_MENTION.finditer(text)]
        operations: list[tuple[CapabilityOperation, str]] = []
        for operation, pattern in _OPERATION_PATTERNS:
            match = pattern.search(text)
            if match:
                operations.append((operation, match.group(0)))
        if not operations:
            operations = [("focus", mentions[0].text)] if mentions else [
                (_DEFAULT_OPERATION[task.domain], "")]

        # Keep each requested operation once while preserving the user's order
        # where textual evidence exists.
        operations = list(dict.fromkeys(operations))
        aspects = [name for name, pattern in _ASPECT_PATTERNS.items() if pattern.search(text)]
        actions = [CapabilityAction(
            action_id=f"{task.task_id}-action-{index}",
            operation=operation,
            aspects=aspects,
            entity_mentions=mentions,
            confidence=task.confidence,
            evidence=evidence or None,
        ) for index, (operation, evidence) in enumerate(operations, 1)]
        return task.model_copy(update={"actions": actions})
