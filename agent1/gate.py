"""质量门控（设计 4.4）：结构完整 / 适用准确 / 证据可追溯 / 变化可解释。

检查结果分级：
- error：隔离（不得发布、不得进入审核）；
- warning：转人工审核（不得自动发布）。

兼容管道的问题条目写入当前环境 legacy/gate/problems.json；新正式结构使用 quality_issues 表。
"""
import json
import os
import re
import uuid
from datetime import datetime, timezone

from . import config
from .models import VISA_FIELDS, GateProblem

# 高风险关键词（设计 4.4：金额、日期、资格、材料、费用、条款、付款信息、官方入口）
HIGH_RISK_KEYWORDS = [
    "费用", "费金", "金额", "学费", "生活费", "存款", "截止", "期限", "资格", "条件",
    "利率", "门槛", "材料", "签证", "入境", "官方", "入口", "条款", "退款", "限额", "持有",
]
CURRENCY_SYMBOLS = {"£": "GBP", "$": "USD", "€": "EUR", "¥": "CNY"}
# 合并后的材料条件（any_of）保留每条条件各自的逐字引用，以此分隔，门控逐条核对。
CONDITION_QUOTE_SEPARATOR = " | "
CONDITION_WORDS = re.compile(r"\b(if|unless|only when)\b|如果|除非", re.I)
STALE_EFFECTIVE_FROM_DAYS = 365
ISO_CURRENCY = re.compile(r"^[A-Z]{3}$")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

NUMERIC_RANGES = {
    "funds_living_monthly": (0, 100000),
    "funds_tuition_unpaid": (0, 1000000),
    "health_surcharge": (0, 100000),
    "visa_fee": (0, 100000),
    "funds_living_months": (1, 60),
    "funds_holding_days": (1, 3650),
    "funds_doc_max_age_days": (1, 3650),
    "apply_window_days": (1, 3650),
    "processing_days": (1, 3650),
}


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def normalize(text):
    return " ".join(text.split())


# 证据引用的比对归一化。
# 模型按页面语义返回「纯文本」，而快照里保留着 Markdown 语法与印刷体标点，
# 两者直接做子串比较会把忠实引用误判为不逐字（实测：’ vs '、[文字](url) vs 文字）。
# 因此比对前对称地做：剥离 Markdown 链接/强调标记 -> 还原 HTML 实体 -> 折叠印刷体标点。
# 必须同时作用于引用与正文两侧，保持比对对称，否则会破坏逐字可追溯性。
_PUNCT_FOLD = {
    "\u2018": "'", "\u2019": "'",          # ‘ ’
    "\u201c": '"', "\u201d": '"',          # “ ”
    "\u2013": "-", "\u2014": "-",          # – —
    "\u2010": "-", "\u2011": "-", "\u2212": "-",
    "\u2026": "...",                        # …
    "\u00a0": " ",                          # 不换行空格
}
_HTML_ENTITIES = {
    "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"',
    "&#39;": "'", "&apos;": "'", "&nbsp;": " ",
}
# [文字](url) 或 [文字](url "标题") -> 文字；标题可含转义括号，如 "\(opens in a new window\)"
_MD_LINK = re.compile(r'\[([^\[\]]*)\]\(\S*?(?:\s+"(?:[^"\\]|\\.)*")?\)')
_MD_EMPHASIS = re.compile(r"[*_`]+")


def comparable(text):
    """把引用/正文归一化为可直接子串比较的形式（两侧必须一致调用）。"""
    text = _MD_LINK.sub(r"\1", text)
    for source, target in _HTML_ENTITIES.items():
        text = text.replace(source, target)
    for source, target in _PUNCT_FOLD.items():
        text = text.replace(source, target)
    text = _MD_EMPHASIS.sub("", text)
    return normalize(text).casefold()


def quote_in_markdown(quote, markdown):
    return len(comparable(quote)) >= 12 and comparable(quote) in comparable(markdown)


_BRACKETED = re.compile(r"\[([^\[\]]+)\]")
_QUALIFIER = re.compile(r"\b(if|unless|only)\b", re.I)


def omits_bracketed_condition(quote, markdown):
    """The quote's line carries a bracketed qualifier ("[if ...]", "[for ... only]") the quote cut off."""
    target = comparable(quote or "")
    if len(target) < 12:
        return False
    for line in markdown.splitlines():
        line = _MD_LINK.sub(r"\1", line)
        if target not in comparable(line):
            continue
        if any(_QUALIFIER.search(q) and comparable(q) not in target for q in _BRACKETED.findall(line)):
            return True
    return False


def condition_quote_in_markdown(quote, markdown):
    """每段条件引用逐字命中；模型把原文列表项用分号拼成一段时，逐项核对。"""
    for part in (quote or "").split(CONDITION_QUOTE_SEPARATOR):
        if quote_in_markdown(part, markdown):
            continue
        items = [item for item in re.split(r";\s*", part) if item.strip()]
        if len(items) < 2 or not all(quote_in_markdown(item, markdown) for item in items):
            return False
    return True


def document_key(document):
    return f"{document.code.strip().casefold()}|{(document.name_zh or '').strip().casefold()}"


# 时长类字段：页面常以「周/月/年」表述，而字段以「天」（或月）存。
# 例：引用 "within 3 weeks" -> processing_days=21，「3 × 7」是确定性单位折算，
# 不是模型凭空推断，因此允许通过，但会记一条 warning 以保持可追溯。
_FIELD_BASE_UNIT = {
    "processing_days": "day",
    "apply_window_days": "day",
    "funds_holding_days": "day",
    "funds_doc_max_age_days": "day",
    "funds_living_months": "month",
}
_TIME_UNIT_DAYS = {
    "day": 1, "days": 1, "week": 7, "weeks": 7, "fortnight": 14, "fortnights": 14,
    "month": 30, "months": 30, "year": 365, "years": 365,
}
# 英文拼写数字：官方页面常写 "six weeks" 而非 "6 weeks"（香港入境处实测），
# 与阿拉伯数字一样参与确定性折算。
_WORD_NUMBERS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}
_DURATION = re.compile(
    r"(?<![\d.])(\d+(?:\.\d+)?|a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s*"
    r"(day|days|week|weeks|fortnight|fortnights|month|months|year|years)\b", re.I)


def _literal_number_in_quote(rendered, quote):
    return re.search(rf"(?<![\d.]){re.escape(rendered)}(?![\d.])",
                     normalize(quote).replace(",", "")) is not None


def number_conversion_note(value, quote, field=None):
    """若取值与引用一致但依赖确定性单位换算，返回可读说明；否则 None。
    引用中出现多个不同时长量级（如 "held 28 days ... within 31 days"）时视为歧义，
    不自动折算——由人工判断，符合"不得由模型换算或推断"的设计底线。"""
    base = _FIELD_BASE_UNIT.get(field)
    if base is None:
        return None
    try:
        target = float(value)
    except (TypeError, ValueError):
        return None
    base_days = _TIME_UNIT_DAYS[base]
    converted = {}
    for match in _DURATION.finditer(normalize(quote).replace(",", "")):
        raw, unit = match.group(1).lower(), match.group(2).lower()
        amount = _WORD_NUMBERS.get(raw)
        if amount is None:
            try:
                amount = float(raw)
            except ValueError:
                return None
        converted[amount * _TIME_UNIT_DAYS[unit]] = (amount, unit, match.group(0))
    if len(converted) != 1:
        return None
    only_days = next(iter(converted))
    amount, _unit, fragment = converted[only_days]
    if abs(target * base_days - only_days) < 1e-9 and abs(amount - target) > 1e-9:
        return f'引用写 "{fragment}"，折算为 {target:g} {base}'
    return None


def number_in_quote(value, quote, field=None):
    """数值必须能在其证据引用中找到（允许千分位差异或确定性单位换算）。"""
    if value is None:
        return True
    try:
        rendered = f"{float(value):g}"
    except (TypeError, ValueError):
        return True
    if _literal_number_in_quote(rendered, quote):
        return True
    return number_conversion_note(value, quote, field=field) is not None


def is_high_risk_text(text):
    if not text:
        return False
    return any(keyword in text for keyword in HIGH_RISK_KEYWORDS)


class GateReport:
    def __init__(self):
        self.problems = []
        self.errors = []
        self.warnings = []
        self.risk_level = "low"

    def problem(self, check, severity, reason, action, **fields):
        entry = {
            "check": check,
            "severity": severity,
            "reason": reason,
            "suggested_action": action,
            **fields,
        }
        self.problems.append(entry)
        (self.errors if severity == "error" else self.warnings).append(entry)
        if severity == "error":
            self.risk_level = "high"

    @property
    def ok(self):
        return not self.errors


# ---------- problems.json 读写 ----------

def _load_file():
    if not config.PROBLEMS_PATH.exists():
        return {"schema": "agent1-gate-problems/1", "problems": []}
    try:
        data = json.loads(config.PROBLEMS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        data = {"schema": "agent1-gate-problems/1", "problems": []}
    data.setdefault("problems", [])
    return data


def _write_file(data):
    config.ensure_dirs()
    data["updated_at"] = now_iso()
    data["open"] = sum(1 for p in data["problems"] if p.get("status") == "open")
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    tmp = config.PROBLEMS_PATH.with_suffix(".json.tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, config.PROBLEMS_PATH)
    except OSError:
        # 沙箱/安全软件可能禁止重命名替换：退化为直接写入目标文件
        try:
            tmp.unlink()
        except OSError:
            pass
        config.PROBLEMS_PATH.write_text(payload, encoding="utf-8")


def append_problems(entries, run_id, source_id, snapshot_hash):
    """把本次运行的门控问题写入统一的 problems.json（按 problem_id 去重）。"""
    if not entries:
        return []
    data = _load_file()
    existing = {p["problem_id"] for p in data["problems"]}
    written = []
    for entry in entries:
        problem_id = str(uuid.uuid5(uuid.NAMESPACE_URL,
                                    f"{run_id}:{entry['check']}:{entry.get('field') or ''}:{entry['reason']}"))
        if problem_id in existing:
            continue
        problem = GateProblem(
            problem_id=problem_id, at=now_iso(), run_id=run_id, source_id=source_id,
            snapshot_hash=snapshot_hash, **entry)
        data["problems"].append(problem.model_dump())
        written.append(problem_id)
        existing.add(problem_id)
    if len(data["problems"]) > config.PROBLEMS_MAX_ENTRIES:
        resolved = [p for p in data["problems"] if p.get("status") == "resolved"]
        keep = data["problems"][-config.PROBLEMS_MAX_ENTRIES:]
        data["problems"] = (resolved + keep)[-config.PROBLEMS_MAX_ENTRIES:]
    _write_file(data)
    return written


def link_run_to_version(run_id, version_id):
    data = _load_file()
    changed = False
    for problem in data["problems"]:
        if problem.get("run_id") == run_id and not problem.get("version_id"):
            problem["version_id"] = version_id
            changed = True
    if changed:
        _write_file(data)


def resolve_problems(problem_ids=None, version_id=None, resolution="resolved"):
    data = _load_file()
    resolved = []
    for problem in data["problems"]:
        if problem.get("status") != "open":
            continue
        if (problem_ids and problem["problem_id"] in problem_ids) or \
           (version_id and problem.get("version_id") == version_id):
            problem["status"] = "resolved"
            problem["resolution"] = f"{resolution} at {now_iso()}"
            resolved.append(problem["problem_id"])
    if resolved:
        _write_file(data)
    return resolved


def load_problems():
    return _load_file()


# ---------- 签证政策门控 ----------

def _evidence_verdict(field, value, evidence, markdown):
    """Why one evidence item fails to support a value ("quote" / "number"); None when it does."""
    if not quote_in_markdown(evidence.evidence_quote, markdown):
        return "quote"
    if isinstance(value, (int, float)) and not isinstance(value, bool) \
            and not number_in_quote(value, evidence.evidence_quote, field=field):
        return "number"
    return None


def _check_visa_entry(report, entry, markdown, scope):
    scope_label = f"[{scope}]"
    policy = entry.policy
    evidence_by_field = {}
    for ev in entry.evidence:
        evidence_by_field.setdefault(ev.field, []).append(ev)
    fields = policy.model_dump(exclude_none=True)

    for field, value in fields.items():
        if field not in VISA_FIELDS:
            report.problem("structure", "error", f"字段 {field} 不在签证 Schema 内",
                           "检查抽取提示词与 Schema 对齐", field=field, scope=scope)
            continue
        candidates = evidence_by_field.get(field)
        if not candidates:
            report.problem("evidence", "error", f"{scope_label} 字段 {field} 缺少字段级证据",
                           "要求模型为每个非空字段提供逐字引用，否则不得采用",
                           field=field, value=str(value), scope=scope)
            continue
        # remark 是模型的补充说明（自由文本），不要求逐字命中页面——
        # 它不参与 business_hash（NOISE_FIELDS），也无需值-引用一致性校验。
        if field == "remark":
            continue
        # 同一字段可能有多条引用（多个章节/页面重复陈述同一取值）：任一条能支撑即通过。
        verdicts = [(ev, _evidence_verdict(field, value, ev, markdown)) for ev in candidates]
        evidence = next((ev for ev, verdict in verdicts if verdict is None), None)
        if evidence is None:
            first, verdict = verdicts[0]
            if verdict == "quote" and all(v == "quote" for _, v in verdicts):
                report.problem("evidence", "error",
                               f"{scope_label} 字段 {field} 的引用未在快照中逐字找到",
                               "核对引用与页面原文；不一致则拒绝该字段",
                               field=field, quote=first.evidence_quote, scope=scope)
            else:
                first = next(ev for ev, v in verdicts if v == "number")
                report.problem("applicability", "error",
                               f"{scope_label} 字段 {field} 的取值未出现在其引用中",
                               "值必须由引用直接支撑，不得由模型换算或推断",
                               field=field, value=str(value), quote=first.evidence_quote,
                               scope=scope)
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            note = number_conversion_note(value, evidence.evidence_quote, field=field)
            if note:
                report.problem("applicability", "warning",
                               f"{scope_label} 字段 {field} 的取值由确定性单位换算得到",
                               f"{note}；确定性归一化，非模型推断，如与页面口径不符请人工核对",
                               field=field, value=str(value), quote=evidence.evidence_quote,
                               scope=scope)

    # 格式与范围
    if fields.get("funds_living_currency") and not ISO_CURRENCY.match(fields["funds_living_currency"]):
        report.problem("structure", "error", "币种不是 ISO 4217 三字母代码",
                       "使用 GBP/USD/EUR 等标准代码", field="funds_living_currency")
    for field in ("effective_from", "effective_to"):
        if fields.get(field) and not ISO_DATE.match(str(fields[field])):
            report.problem("structure", "error", f"{field} 不是 ISO 日期格式",
                           "使用 YYYY-MM-DD", field=field, value=str(fields[field]))
    if fields.get("effective_from") and ISO_DATE.match(str(fields["effective_from"])):
        started = datetime.strptime(str(fields["effective_from"])[:10], "%Y-%m-%d").date()
        if (datetime.now(timezone.utc).date() - started).days > STALE_EFFECTIVE_FROM_DAYS:
            report.problem("applicability", "error",
                           f"{scope_label} effective_from 是一年以前的历史日期，不是当前政策的生效日",
                           "页面上的历史日期（表格改版、旧规生效等）不填 effective_from",
                           field="effective_from", value=str(fields["effective_from"]), scope=scope)
    for field, (low, high) in NUMERIC_RANGES.items():
        if field in fields and not (low <= float(fields[field]) <= high):
            report.problem("structure", "error", f"{field} 取值超出合理范围 [{low}, {high}]",
                           "人工核对页面原文", field=field, value=str(fields[field]))

    # 适用准确
    if "funds_living_monthly" in fields and not fields.get("applies_to_city"):
        report.problem("applicability", "warning", "金额字段未标注适用范围（applies_to_city）",
                       "按页面表述补充适用范围，不同范围应拆分为不同条目", field="applies_to_city")
    quote_currency = None
    for evidence in entry.evidence:
        for symbol, code in CURRENCY_SYMBOLS.items():
            if symbol in evidence.evidence_quote:
                quote_currency = code
    if fields.get("funds_living_currency") and quote_currency and quote_currency != fields["funds_living_currency"]:
        report.problem("applicability", "error", "币种与引用中的货币符号不一致",
                       "核对页面原始币种", field="funds_living_currency")
    elif fields.get("funds_living_currency") and quote_currency == fields["funds_living_currency"] \
            and fields["funds_living_currency"] not in " ".join(ev.evidence_quote for ev in entry.evidence):
        report.problem("applicability", "warning", "币种由货币符号推断，引用中无代码原文",
                       "可接受为归一化，请在证据 note 中注明推断依据",
                       field="funds_living_currency", value=fields["funds_living_currency"])


def _check_visa_documents(report, patch, markdown):
    for document in patch.documents:
        if not document.code or not document.name_zh:
            report.problem("structure", "error", "文档条目缺少 code 或 name_zh",
                           "补齐文档编码与中文名", field=f"document:{document.code}")
            continue
        key = document_key(document)
        if not quote_in_markdown(document.evidence_quote, markdown):
            report.problem("evidence", "error", f"文档 {document.code} 的引用未在快照中逐字找到",
                           "核对引用与页面原文", field=f"document:{document.code}",
                           quote=document.evidence_quote, document=key, part="evidence")
        if document.condition and not document.condition_quote:
            report.problem("evidence", "error",
                           f"文档 {document.code} 的 condition 缺少逐字引用（condition_quote）",
                           "condition 必须有原文支撑，无法支撑时删除 condition 并将该条转人工",
                           field=f"document:{document.code}", document=key, part="condition")
        elif document.condition and not condition_quote_in_markdown(document.condition_quote, markdown):
            report.problem("evidence", "error",
                           f"文档 {document.code} 的 condition_quote 未在快照中逐字找到",
                           "condition 的适用条件必须由引用直接支撑",
                           field=f"document:{document.code}", quote=document.condition_quote,
                           document=key, part="condition")
        if not document.condition and CONDITION_WORDS.search(document.evidence_quote):
            report.problem("applicability", "warning",
                           f"文档 {document.code} 的引用包含条件，但抽取结果缺少 condition",
                           "从原文提取适用条件并复核", field=f"document:{document.code}")


def _check_visa_scoping(report, patch, markdown):
    cities = [entry.policy.applies_to_city for entry in patch.policies if entry.policy.applies_to_city]
    if len(cities) != len(set(cities)):
        report.problem("applicability", "error", "存在重复适用范围的政策条目",
                       "同一适用范围只保留一条", field="applies_to_city")
    if "outside london" in normalize(markdown).casefold() and \
            not any(city and "outside" in city.casefold() for city in cities):
        report.problem("applicability", "warning",
                       "页面含非伦敦适用范围金额，但抽取结果未拆分出该范围",
                       "按适用范围拆分条目，避免把不同范围金额混在一条政策里",
                       field="applies_to_city")


def verify_visa_evidence(patch, markdown, report=None):
    """Field/document evidence checks only (no page-level scoping), for one section patch."""
    report = report or GateReport()
    for index, entry in enumerate(patch.policies, start=1):
        city = entry.policy.applies_to_city or f"条目{index}"
        _check_visa_entry(report, entry, markdown, city)
    _check_visa_documents(report, patch, markdown)
    return report


def run_gate_visa(patch, markdown, source, meta):
    report = GateReport()
    report.risk_level = "high"  # 签证政策整体按高风险治理（ADR-002）
    verify_visa_evidence(patch, markdown, report)
    _check_visa_scoping(report, patch, markdown)
    # excerpt_chars=0 表示规则降级路径（未调用模型），不应报截断
    if meta.get("excerpt_chars") and len(normalize(markdown)) > meta["excerpt_chars"] + 500:
        report.problem("change", "warning",
                       f"正文 {len(normalize(markdown))} 字符超出模型截取范围 "
                       f"{meta.get('excerpt_chars')} 字符，截取外内容未核对",
                       "扩大截取或分块抽取后复核，防止遗漏例外条款", field="excerpt")
    return report


# ---------- 通用事实门控 ----------

def run_gate_facts(extraction, markdown, source, meta):
    report = GateReport()
    for fact in extraction.facts:
        label = f"[{fact.topic}]"
        if not fact.topic.strip() or not fact.value.strip():
            report.problem("structure", "error", f"{label} topic 或 value 为空",
                           "删除空事实或要求模型补充", field=fact.topic)
            continue
        if not quote_in_markdown(fact.evidence_quote, markdown):
            report.problem("evidence", "error", f"{label} 的引用未在快照中逐字找到",
                           "核对引用与页面原文；不一致则拒绝该事实",
                           field=fact.topic, quote=fact.evidence_quote)
        if is_high_risk_text(f"{fact.topic} {fact.value}"):
            report.risk_level = "high"
    # excerpt_chars=0 表示规则降级路径（未调用模型），不应报截断
    if meta.get("excerpt_chars") and len(normalize(markdown)) > meta["excerpt_chars"] + 500:
        report.problem("change", "warning",
                       f"正文 {len(normalize(markdown))} 字符超出模型截取范围 "
                       f"{meta.get('excerpt_chars')} 字符，截取外内容未核对",
                       "扩大截取或分块抽取后复核", field="excerpt")
    return report


def run_gate(patch, markdown, source, meta):
    schema_type = source.get("schema_type", "generic_facts")
    if schema_type == "visa_policy":
        return run_gate_visa(patch, markdown, source, meta)
    return run_gate_facts(patch, markdown, source, meta)
