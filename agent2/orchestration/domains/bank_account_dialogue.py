"""Deterministic multi-turn dialogue state for the bank-account customer service.

The model may help classify the banking question, but references such as ``这个``
and control moves such as ``别的呢`` must be reduced against explicit session
state.  Keeping this state out of Profile also avoids turning a product mention
into a durable profile-change proposal.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


DialogueAct = Literal[
    "discover_options", "focus_product", "request_alternatives",
    "ask_catalog_scope", "compare_products", "confirm_selection",
    "reject_product", "ask_process", "ask_eligibility", "ask_documents",
    "ask_fees", "ask_necessity", "troubleshoot", "correct_fact", "other",
]
AnswerMode = Literal["single", "compare", "catalog", "process", "troubleshoot"]


class AccountConversationState(BaseModel):
    model_config = ConfigDict(extra="ignore")

    market: str | None = None
    goal: Literal["daily_banking", "savings", "fixed_deposit", "unknown"] = "unknown"
    stage: Literal["discovering", "exploring", "selected", "opening", "troubleshooting"] = \
        "discovering"
    candidate_product_ids: list[str] = Field(default_factory=list)
    presented_product_ids: list[str] = Field(default_factory=list)
    focused_product_id: str | None = None
    rejected_product_ids: list[str] = Field(default_factory=list)
    selected_product_id: str | None = None
    unresolved_fields: list[str] = Field(default_factory=list)
    last_dialogue_act: str | None = None
    last_answer_mode: str | None = None
    version: int = 0


def load_account_state(raw: dict[str, Any] | None,
                       market_hint: str | None = None) -> AccountConversationState:
    """Load one market branch while accepting the legacy flat state shape."""
    data = raw or {}
    markets = data.get("markets") if isinstance(data, dict) else None
    if isinstance(markets, dict):
        market = market_hint or data.get("active_market")
        branch = markets.get(market) if market else None
        if isinstance(branch, dict):
            return AccountConversationState.model_validate(branch)
        return AccountConversationState(market=market_hint)
    return AccountConversationState.model_validate(data)


def pack_account_state(raw: dict[str, Any] | None,
                       current: AccountConversationState) -> dict[str, Any]:
    """Persist independent branches per market and keep flat compatibility keys."""
    previous = raw or {}
    markets = dict(previous.get("markets") or {}) if isinstance(previous, dict) else {}
    if not markets and isinstance(previous, dict) and previous.get("market"):
        legacy = AccountConversationState.model_validate(previous).model_dump(mode="json")
        markets[legacy["market"]] = legacy
    branch = current.model_dump(mode="json")
    if current.market:
        markets[current.market] = branch
    return {**branch, "active_market": current.market, "markets": markets}


def infer_goal(text: str, previous: str = "unknown") -> str:
    folded = text.casefold()
    if re.search(r"定期|存单|\bcd\b|fixed", folded, re.I):
        return "fixed_deposit"
    if re.search(r"储蓄|存钱|利息|savings?", folded, re.I):
        return "savings"
    if re.search(r"日常|收付款|工资|交学费|生活费|消费|支票|往来|checking|current", folded, re.I):
        return "daily_banking"
    return previous


def classify_dialogue_act(text: str, *, named_product: str | None,
                          state: AccountConversationState) -> DialogueAct:
    compact = re.sub(r"\s+", "", text.casefold())
    if re.search(r"说错了|不是.*是|改成|更正|刚才.*错", compact):
        return "correct_fact"
    if re.search(r"只有这|只有[^，。]{0,10}(?:这种|一种|这几种)|就这(?:些|几|两|俩)|一共(?:有)?(?:哪些|几个)|全部账户|完整(?:列表|产品)|"
                 r"还有(?:别的|其他).*(?:吗|么)", compact):
        return "ask_catalog_scope"
    if re.search(r"要不要.*(?:开户|开账户)|需不需要.*(?:开户|开账户)|是否需要.*(?:开户|开账户)|"
                 r"是否一定.*(?:开户|开账户)|需要先开户|要先开户|必须先开户|有必要.*(?:开户|开账户)|"
                 r"(?:出发|抵达|到美国|到英国|到香港)前.*(?:要不要|需不需要|是否需要).*(?:开户|开账户)",
                 compact):
        return "ask_necessity"
    if re.search(r"不要这个|不考虑这个|排除这个|这个不行|不选这个", compact):
        return "reject_product"
    if re.search(r"别的|其他|另外|换一个|下一个|之外", compact):
        return "request_alternatives"
    if re.search(r"就这个|选这个|办这个|开这个|就它|决定(?:选|办|开)|就(?:选|办|开)", compact):
        return "confirm_selection"
    if re.search(r"比较|对比|区别|不同|差异|哪个好|哪一个好|还是", compact):
        return "compare_products"
    if re.search(r"(?:有什么|有哪些|什么|哪些)账户(?:我)?(?:可以|能)?开?|"
                 r"开(?:什么|哪种)户|(?:想|(?<!需)要|打算|准备|计划)开[^，。]{0,10}(?:户|账户)", compact):
        return "discover_options"
    if re.search(r"失败|被拒|过不去|提交不了|补交|没消息|取消|审核未通过|识别不了|报错", compact):
        return "troubleshoot"
    if re.search(r"费用|收费|多少钱|月费|管理费|最低存款|最低要存|要存.*钱|最低余额|结余|豁免|利率|门槛", compact):
        return "ask_fees"
    if re.search(r"材料|证件|证明|要带什么|准备什么", compact):
        return "ask_documents"
    if re.search(r"能不能|能否|能开|可以.*开|资格|条件|几岁|未满|要住满", compact):
        return "ask_eligibility"
    if re.search(r"怎么开|如何开|怎么申请|办理|预约|注意(?:什么|事项)|线上|在线|网上|网点|分行|app", compact):
        return "ask_process"
    if named_product:
        return "focus_product"
    if re.search(r"什么户|哪种户|什么账户|哪种账户|怎么选|适合|有哪些|储蓄账户|"
                 r"定期存款|支票账户|往来账户", compact):
        return "discover_options"
    return "other"


def answer_mode_for(act: DialogueAct) -> AnswerMode:
    if act in {"ask_catalog_scope", "request_alternatives", "discover_options", "reject_product"}:
        return "catalog"
    if act == "compare_products":
        return "compare"
    if act == "troubleshoot":
        return "troubleshoot"
    if act in {"focus_product", "confirm_selection"}:
        return "single"
    return "process"


def exclusion_ids(state: AccountConversationState, act: DialogueAct) -> set[str]:
    excluded = set(state.rejected_product_ids)
    if act in {"request_alternatives", "reject_product"}:
        excluded.update(state.presented_product_ids)
        if state.focused_product_id:
            excluded.add(state.focused_product_id)
    return excluded


def reduce_account_state(previous: AccountConversationState, *, market: str,
                         act: DialogueAct, mode: AnswerMode,
                         named_product: str | None,
                         shown_product_ids: list[str],
                         candidate_product_ids: list[str] | None = None,
                         unresolved_fields: list[str] | None = None) -> AccountConversationState:
    state = previous.model_copy(deep=True)
    if state.market and state.market != market:
        state = AccountConversationState()
    state.market = market
    state.goal = infer_goal("", state.goal)
    state.last_dialogue_act = act
    state.last_answer_mode = mode
    state.version += 1

    if act in {"request_alternatives", "reject_product"}:
        rejected = [*state.rejected_product_ids, *state.presented_product_ids]
        if state.focused_product_id:
            rejected.append(state.focused_product_id)
        state.rejected_product_ids = list(dict.fromkeys(rejected))
        state.focused_product_id = None
        state.selected_product_id = None

    if named_product:
        state.focused_product_id = named_product
        state.rejected_product_ids = [item for item in state.rejected_product_ids
                                      if item != named_product]
    elif len(shown_product_ids) == 1 and act in {
        "focus_product", "ask_process", "ask_documents", "ask_eligibility", "ask_fees", "troubleshoot"
    }:
        state.focused_product_id = shown_product_ids[0]

    if act == "confirm_selection":
        chosen = named_product or state.focused_product_id or (
            shown_product_ids[0] if len(shown_product_ids) == 1 else None)
        if chosen:
            state.selected_product_id = chosen
            state.focused_product_id = chosen
            state.stage = "selected"
    elif act == "troubleshoot":
        state.stage = "troubleshooting"
    elif act in {"ask_process", "ask_documents"} and state.focused_product_id:
        state.stage = "opening"
    elif act in {"discover_options", "ask_catalog_scope", "request_alternatives",
                 "compare_products", "reject_product", "focus_product"}:
        state.stage = "exploring"

    if act != "ask_necessity":
        state.presented_product_ids = list(dict.fromkeys(shown_product_ids))
    state.candidate_product_ids = list(dict.fromkeys(candidate_product_ids or shown_product_ids))
    state.unresolved_fields = list(dict.fromkeys(unresolved_fields or []))
    return state


# Product-to-product relationships that materially change how a product should be
# explained.  They are intentionally separate from eligibility rules: these rows
# describe composition/dependency, not whether the user passes a criterion.
PRODUCT_RELATIONSHIPS: dict[str, list[dict[str, str]]] = {
    "hsbc_us_global_money": [{
        "type": "requires_relationship",
        "target": "HSBC Premier checking or savings relationship",
        "message": "它是面向现有 HSBC Premier 关系客户的补充多币种账户，不是普通支票账户的替代品。",
    }],
    "hsbc_us_premier_savings": [{
        "type": "companion_to",
        "target": "HSBC Premier Checking",
        "message": "它是 Premier 关系下的储蓄账户，通常要和 Premier Checking 关系一起理解。",
    }],
    "hsbc_us_premier_relationship_savings": [{
        "type": "companion_to",
        "target": "HSBC Premier Checking",
        "message": "它属于 Premier 关系储蓄产品，不能只按独立日常账户来比较。",
    }],
    "hsbc_us_new_money_cd": [{
        "type": "companion_to",
        "target": "HSBC Premier Checking",
        "message": "它是定期存款产品，适合存放一段时间内不动用的资金，不承担日常收付款。",
    }],
    "hsbc_us_online_cd": [{
        "type": "companion_to",
        "target": "HSBC Premier Checking",
        "message": "它是线上定期存款产品，不是日常消费和收付款账户。",
    }],
    "hsbc_hk_premier_elite": [{
        "type": "tier_of",
        "target": "HSBC Premier",
        "message": "它是 HSBC Premier 之上的服务层级，不应当作普通入门账户单独比较。",
    }],
}


def product_relationships(account_id: str) -> list[dict[str, str]]:
    return [dict(item) for item in PRODUCT_RELATIONSHIPS.get(account_id, [])]


def relationship_messages(relationships: list[dict[str, str]]) -> list[str]:
    """Turn normalized relationship rows into concise customer-facing facts."""
    messages: list[str] = []
    required = [row.get("target_product_name") or row.get("target")
                for row in relationships if row.get("relation_type") == "requires_relationship"
                or row.get("type") == "requires_relationship"]
    required = [item for item in required if item]
    if len(required) > 1:
        messages.append("它需要先有合资格的汇丰账户关系；当前资料列出的可用关系包括："
                        + "、".join(dict.fromkeys(required)) + "。")
    for row in relationships:
        if len(required) > 1 and (row.get("relation_type") == "requires_relationship"
                                  or row.get("type") == "requires_relationship"):
            continue
        message = row.get("message")
        if message and message not in messages:
            messages.append(message)
    return messages
