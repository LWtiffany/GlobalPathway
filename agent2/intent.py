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
# 不用裸「刷卡」（留给外汇境外支付）；不用裸 visa（留给学签）。
_CREDIT_CARD = re.compile(
    r"信用卡|credit\s*cards?"
    r"|威士卡|万事达(?:卡)?"
    r"|(?<![a-z])visa\s*卡|(?<![a-z])mastercard(?![a-z])"
    r"|办(?:一张)?(?:信用)?卡|开卡|申卡|办卡"
    r"|刷卡优惠|信用卡优惠|开卡优惠|卡优惠"
    r"|联名卡|白金卡|世界之极|world\s*elite"
    r"|哪张卡|什么卡|卡推荐|推荐.+?卡|留学生(?:信用)?卡"
    r"|年费|积分里程|里程积分|(?:刷卡|消费)积分",
    re.I,
)

# 按优先级匹配；out_of_scope 优先，避免"帮我选校顺便问签证"被当作签证问答直接回答
# classify() 取列表中第一个命中；find_intent_matches() 收集全部命中供多任务规划。
INTENTS = [
    ("out_of_scope", re.compile(r"选校|帮我申请学校|代办|租房合同|合同审查|股票|基金|炒币|理财产品")),
    # visa 卡 / Visa卡 走信用卡；学签仍用 visa / cas 单词。
    ("visa_documents", re.compile(
        r"签证|学签|(?<![a-z])visa(?!\s*卡)(?![a-z])|(?<![a-z])cas(?![a-z])", re.I)),
    ("remittance_tuition", re.compile(
        r"学费|tuition(?:\s*fees?)?|缴学费|交学费|学费汇", re.I)),
    ("remittance_living", re.compile(
        r"生活费|生活开支|生活费汇|living\s*(?:cost|expense|fee)s?"
        r"|(?<!学费)汇款", re.I)),
    ("hsbc_account", re.compile(
        r"开户|银行账户|银行开户|开银行|开汇丰|汇丰账户|学生账户"
        r"|bank\s*accounts?|open(?:ing)?\s+(?:a\s+)?(?:bank\s+)?account", re.I)),
    ("credit_card", _CREDIT_CARD),
    # 裸「刷卡」仅在境外/海外语境；换汇/购汇/汇率归外汇咨询
    ("overseas_spending", re.compile(
        r"境外消费|海外消费|(?:境外|海外).{0,8}刷卡|怎么刷卡"
        r"|换汇|购汇|结汇|外汇|汇率|foreign\s*exchange|\bfx\b", re.I)),
]


def find_intent_matches(text: str) -> list[tuple[int, str]]:
    """Return (start, intent) for every INTENTS hit, in first-occurrence order within each pattern."""
    matches: list[tuple[int, str]] = []
    for intent, pattern in INTENTS:
        match = pattern.search(text)
        if match:
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
    intent = next((name for name, pattern in INTENTS if pattern.search(text)), None)
    turn = Turn(intent=intent or "other")
    for code, patterns in (("destination_country", _COUNTRY), ("visa_category", _VISA),
                           ("funding_source", _FUNDING)):
        value, basis = _first(patterns, text)
        if value:
            turn.facts[code], turn.basis[code] = value, basis
    age = _AGE.search(text)
    if age:
        turn.facts["applicant_age"], turn.basis["applicant_age"] = int(age.group(1)), age.group(0)
    school = _SCHOOL.search(text)
    if school:
        turn.facts["school"], turn.basis["school"] = school.group(1), school.group(0)
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
