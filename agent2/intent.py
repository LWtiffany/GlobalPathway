"""M05 意图分类与本轮条件提取（S1：确定性规则，不调用模型）。

提取出的条件只作本会话临时条件，并生成档案变更提案（M03），从不直接写入 Profile。
用户只回答"是/否"时，仅在上一轮恰好问了一个是/否字段时才绑定到该字段。
"""
import re
from dataclasses import dataclass, field

from .clarify import BOOLEAN_FIELDS

# 按优先级匹配；out_of_scope 优先，避免"帮我选校顺便问签证"被当作签证问答直接回答
INTENTS = [
    ("out_of_scope", re.compile(r"选校|帮我申请学校|代办|租房合同|合同审查|股票|基金|炒币|理财产品")),
    ("visa_documents", re.compile(r"签证|学签|(?<![a-z])(?:visa|cas)(?![a-z])", re.I)),
    ("remittance_tuition", re.compile(r"学费")),
    ("remittance_living", re.compile(r"生活费")),
    ("hsbc_account", re.compile(r"开户|账户")),
    ("credit_card", re.compile(r"信用卡")),
    ("overseas_spending", re.compile(r"境外消费|海外消费|刷卡")),
]

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
    purpose = {"remittance_tuition": "tuition", "remittance_living": "living"}.get(turn.intent)
    if purpose:
        turn.facts["remittance_purpose"] = purpose
        turn.basis["remittance_purpose"] = "学费" if purpose == "tuition" else "生活费"
    if turn.intent == "other" and turn.facts:
        turn.intent = "follow_up"
    return turn
