"""M05 意图分类与本轮条件提取（S1：确定性规则，不调用模型）。

提取出的条件只作本会话临时条件，并生成档案变更提案（M03），从不直接写入 Profile。
用户只回答"是/否"时，仅在上一轮恰好问了一个是/否字段时才绑定到该字段。
上一轮问的是日期字段时，从自由文本里解析 YYYY-MM-DD / 中文日期并绑定。
"""
import re
from dataclasses import dataclass, field
from datetime import date

from .clarify import BOOLEAN_FIELDS, FIELDS, _iso_date

# 信用卡 paraphrases：覆盖「Visa 卡推荐 / 刷卡优惠 / 办卡」等，避免只靠「信用卡」二字。
# 裸「办卡 / 开卡 / 申卡」虽然能命中候选，但会由 is_ambiguous_bank_card
# 拦下并让用户选择账户/借记卡还是信用卡，不能直接猜成信用卡。
# 不用裸「刷卡」（留给外汇境外支付）；不用裸 visa（留给学签）。
_CREDIT_CARD = re.compile(
    r"信用卡|credit\s*cards?"
    r"|威士卡|万事达(?:卡)?"
    r"|(?<![a-z])visa\s*卡|(?<![a-z])mastercard(?![a-z])"
    r"|办(?:一?张)?(?:信用)?卡|开卡|申卡|办卡"
    r"|刷卡优惠|信用卡优惠|开卡优惠|卡优惠"
    r"|联名卡|白金卡|世界之极|world\s*elite"
    r"|哪张卡|什么卡|卡推荐|推荐.+?卡|留学生(?:信用)?卡"
    r"|年费|积分里程|里程积分|(?:刷卡|消费)积分",
    re.I,
)

# Explicit everyday-account wording. Bare「银行卡」is intentionally absent: it
# can mean a debit card or a credit card and needs a short clarification.
_BANK_ACCOUNT = re.compile(
    r"开户|银行账户|银行开户|开银行|开汇丰|汇丰账户|"
    r"汇丰(?:的|都?有|还?有)?(?:哪些|什么|其他|别的)?账户|学生账户"
    r"|(?:有什么|有哪些|什么|哪些)账户(?:我)?(?:可以|能)?开?"
    r"|开(?:什么|哪种|哪类)户|(?:什么|哪种|哪类)户(?:比较好|适合|推荐)"
    r"|账户(?:推荐|建议)|账户(?:类型|种类|产品)(?:推荐|建议|怎么选|有哪些|有什么)?"
    r"|(?:别的|其他)账户(?:推荐|建议|类型|种类|产品)?"
    r"|(?:推荐|建议|帮我选)(?:一[个张]|[个张])?(?:新)?账户"
    r"|(?<![打离开])开(?:一[个张]|[个张])?[^，。,.!！?？]{0,12}账户"
    r"|(?<![打离开])开[^，。,.!！?？]{0,8}的?户"
    r"|(?:能|可以|想|要|可)开户|(?:能|可以)开吗|开户吗|开不了户"
    r"|储蓄(?:卡|账户|户口)|借记卡|提款卡|(?:ATM|提款)卡"
    r"|往来账户|活期账户|支票账户|普通(?:银行)?账户"
    r"|HSBC\s*One|Premier\s*Checking|Premier\s*(?:Relationship\s*)?Savings"
    r"|Global\s*Money(?:\s*Account)?|(?:New\s*Money|Online)\s*CD"
    r"|Student\s*Bank\s*Account|HSBC\s*Teens?|Premier\s*Elite"
    r"|bank\s*accounts?|open(?:ing)?\s+(?:a\s+)?(?:bank\s+)?account"
    r"|debit\s*card|checking\s*account|savings?\s*account",
    re.I,
)
_EXPLICIT_CREDIT_CARD = re.compile(
    r"信用卡|credit\s*cards?|(?<![a-z])visa\s*卡|(?<![a-z])mastercard(?![a-z])"
    r"|威士卡|万事达(?:卡)?|联名卡|白金卡|世界之极|world\s*elite"
    r"|刷卡优惠|开卡优惠|积分里程|里程积分|(?:刷卡|消费)积分",
    re.I,
)
_GENERIC_CARD_REQUEST = re.compile(
    r"银行卡|办(?:一?张)?卡|开卡|申卡|想(?:办|要)(?:一?张)?卡"
    r"|哪张卡|什么卡|卡推荐|推荐.+?卡|留学生卡",
    re.I,
)
_INVESTMENT_ACCOUNT = re.compile(
    r"股票账户|证券账户|证券交易账户|炒股账户|股票交易账户|券商账户|"
    r"brokerage\s+account|securities\s+account|stock\s+trading\s+account",
    re.I,
)


def is_investment_account(text: str) -> bool:
    """Investment/brokerage accounts are not deposit/current accounts."""
    return bool(_INVESTMENT_ACCOUNT.search(text))


def is_ambiguous_bank_card(text: str) -> bool:
    """A generic card request may mean a debit card or a credit card."""
    return bool(_GENERIC_CARD_REQUEST.search(text) and not _BANK_ACCOUNT.search(text)
                and not _EXPLICIT_CREDIT_CARD.search(text))


def is_account_only_card_mention(text: str) -> bool:
    """Suppress generic card-benefit words when an explicit deposit account is named."""
    return bool(_BANK_ACCOUNT.search(text) and not _EXPLICIT_CREDIT_CARD.search(text))

# 按优先级匹配；out_of_scope 优先，避免"帮我选校顺便问签证"被当作签证问答直接回答
# classify() 取列表中第一个命中；find_intent_matches() 收集全部命中供多任务规划。
INTENTS = [
    ("out_of_scope", re.compile(
        r"选校|帮我申请学校|代办|租房合同|合同审查|股票|证券(?:交易)?账户|券商账户|"
        r"基金|炒币|理财产品|brokerage\s+account|securities\s+account", re.I)),
    # visa 卡 / Visa卡 走信用卡；学签仍用 visa / cas 单词。
    ("visa_documents", re.compile(
        r"签证|学签|(?<![a-z])visa(?!\s*卡)(?![a-z])|(?<![a-z])cas(?![a-z])", re.I)),
    ("remittance_tuition", re.compile(
        r"学费|tuition(?:\s*fees?)?|缴学费|交学费|学费汇"
        r"|缴费通知|学费账单|录取通知"
        r"|收款(?:人|信息|账户|账号)|SWIFT|汇款信息"
        r"|代汇|父母.{0,16}(?:汇|交|付).{0,12}学费"
        r"|留学购汇|购汇额度|超额购汇|便利化额度|年度(?:购汇)?额度|超年度", re.I)),
    ("remittance_living", re.compile(
        r"生活费|生活开支|生活费汇|living\s*(?:cost|expense|fee)s?"
        r"|生活费.{0,8}(?:购汇|额度|安全)|孩子.{0,12}生活费"
        r"|(?<!学费)汇款", re.I)),
    ("hsbc_account", _BANK_ACCOUNT),
    ("credit_card", _CREDIT_CARD),
    # 境外刷卡/换汇；裸「购汇/额度」已由学费/生活费意图优先承接
    ("overseas_spending", re.compile(
        r"境外消费|海外消费|(?:境外|海外).{0,8}刷卡|怎么刷卡"
        r"|换汇|结汇|外汇|汇率|foreign\s*exchange|\bfx\b|购汇", re.I)),
]


_ORDER_FLOW = re.compile(r"先后|顺序|先办|再办|先做|下一步", re.I)


def find_intent_matches(text: str) -> list[tuple[int, str]]:
    """Return (start, intent) for every INTENTS hit, in first-occurrence order within each pattern."""
    matches: list[tuple[int, str]] = []
    order_flow = bool(_ORDER_FLOW.search(text))
    for intent, pattern in INTENTS:
        match = pattern.search(text)
        if match:
            if intent == "hsbc_account" and is_investment_account(text):
                continue
            if intent == "credit_card" and (is_ambiguous_bank_card(text)
                                           or is_account_only_card_mention(text)):
                # 「交学费 / 开户 / 办卡先后」允许把办卡算作信用卡域，用于顺序建议。
                if not (order_flow and re.search(r"办卡|信用卡", text)):
                    continue
            matches.append((match.start(), intent))
    return matches

_COUNTRY = [
    (re.compile(r"英国|英格兰|苏格兰|(?<![A-Za-z])UK(?![A-Za-z])|United Kingdom|Britain"), "UK"),
    (re.compile(r"美国|(?<![A-Za-z])USA?(?![A-Za-z])|United States|America"), "US"),
    (re.compile(r"香港|Hong\s*Kong", re.I), "CN-HK"),
]
_VISA = [
    (re.compile(r"student\s*visa|学生签证?|学签|留学签证?|f-?1\s*visa", re.I), "student_visa"),
    (re.compile(r"visitor\s*visa|旅游签证?|访问签证?|探亲签证?", re.I), "visitor_visa"),
    (re.compile(r"graduate\s*visa|毕业生签证?|(?<![a-z])psw(?![a-z])", re.I), "graduate_visa"),
    (re.compile(r"dependant\s*visa|家属签证?|陪读签证?", re.I), "dependant_visa"),
]
_FUNDING = [
    (re.compile(r"学生贷款|助学贷款|student\s*loan", re.I), "student_loan"),
    (re.compile(r"奖学金|scholarship", re.I), "scholarship"),
    (re.compile(r"资助|赞助|sponsor", re.I), "sponsor"),
    (re.compile(r"自费|父母(?:出钱|支付|的存款)|家里出钱|自己的存款", re.I), "own_or_parents"),
]
_PAYER = [
    (re.compile(r"父母|家长|爸爸|妈妈|代汇|代办"), "parent"),
    (re.compile(r"本人|自己(?:汇|付|交)|学生本人|我来(?:汇|付|交)"), "self"),
]
_AGE = re.compile(r"(\d{1,3})\s*(?:岁|周岁)")
_SCHOOL = re.compile(r"(?:学校(?:改成|换成|改为|是|为)|被)\s*([^\s，。,.!！?？]+?)(?:\s*录取|[，。,.!！?？]|$)")
_YES = re.compile(r"^\s*(?:是|是的|对|对的|有|需要|yes|y)\s*[。.!！]?\s*$", re.I)
_NO = re.compile(r"^\s*(?:否|不是|没有|不需要|不|no|n)\s*[。.!！]?\s*$", re.I)
_DATE_ISO = re.compile(r"(\d{4}-\d{2}-\d{2})")
_DATE_CN = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_DATE_SLASH = re.compile(r"(\d{4})[/.](\d{1,2})[/.](\d{1,2})")


def _parse_date(text):
    match = _DATE_ISO.search(text)
    if match:
        value = match.group(1)
        return (value, match.group(0)) if _iso_date(value) is None else (None, None)
    match = _DATE_CN.search(text) or _DATE_SLASH.search(text)
    if not match:
        return None, None
    try:
        value = date(int(match.group(1)), int(match.group(2)), int(match.group(3))).isoformat()
    except ValueError:
        return None, None
    return value, match.group(0)


@dataclass
class Turn:
    intent: str
    facts: dict = field(default_factory=dict)
    basis: dict = field(default_factory=dict)  # field -> 原文片段


def _first(patterns, text):
    for pattern, value in patterns:
        match = pattern.search(text)
        if match:
            return value, match.group(0)
    return None, None


def classify(text, *, asked_fields=()):
    # Single-label: INTENTS 表顺序优先（out_of_scope 压过业务域）。多标签见 find_intent_matches。
    intent = next((name for name, pattern in INTENTS if pattern.search(text)
                   and not (name == "credit_card" and
                            (is_ambiguous_bank_card(text) or is_account_only_card_mention(text)))), None)
    turn = Turn(intent=intent or "other")
    for code, patterns in (("destination_country", _COUNTRY), ("visa_category", _VISA),
                           ("funding_source", _FUNDING), ("payer", _PAYER)):
        value, basis = _first(patterns, text)
        if value:
            turn.facts[code], turn.basis[code] = value, basis
    age = _AGE.search(text)
    if age:
        turn.facts["applicant_age"], turn.basis["applicant_age"] = int(age.group(1)), age.group(0)
    school = _SCHOOL.search(text)
    if school:
        turn.facts["school"], turn.basis["school"] = school.group(1), school.group(0)
    # Free-text may already include a tuition deadline date.
    deadline, deadline_basis = _parse_date(text)
    if deadline and re.search(r"截止|付款日|缴费日|deadline", text, re.I):
        turn.facts["payment_deadline"] = deadline
        turn.basis["payment_deadline"] = deadline_basis
    boolean_asked = [f for f in asked_fields if f in BOOLEAN_FIELDS]
    if len(boolean_asked) == 1 and (_YES.match(text) or _NO.match(text)):
        turn.facts[boolean_asked[0]] = bool(_YES.match(text))
        turn.basis[boolean_asked[0]] = text.strip()
    date_asked = [f for f in asked_fields if f in FIELDS and FIELDS[f][1] is _iso_date]
    if len(date_asked) == 1:
        value, basis = _parse_date(text)
        if value:
            turn.facts[date_asked[0]], turn.basis[date_asked[0]] = value, basis
    purpose = {"remittance_tuition": "tuition", "remittance_living": "living"}.get(turn.intent)
    if purpose:
        turn.facts["remittance_purpose"] = purpose
        turn.basis["remittance_purpose"] = "学费" if purpose == "tuition" else "生活费"
    # 「去美国读书 / 留学签证材料」未点名签证类别时，按留学场景推断学生签证。
    if (turn.intent == "visa_documents" and "visa_category" not in turn.facts
            and re.search(r"读书|留学|就读|上学|录取|学校|大学|研究生|硕士|本科", text)):
        turn.facts["visa_category"] = "student_visa"
        turn.basis["visa_category"] = "留学/读书场景"
    if turn.intent == "other" and turn.facts:
        turn.intent = "follow_up"
    return turn
