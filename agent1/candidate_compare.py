"""Deterministic business comparison; LLM omissions never delete accepted facts."""
import json
import re
from dataclasses import dataclass
from decimal import Decimal


_NUMBER_FIELDS = {"funds_tuition_unpaid", "funds_living_monthly", "funds_living_months",
                  "funds_holding_days", "funds_doc_max_age_days", "health_surcharge",
                  "apply_window_days", "processing_days", "visa_fee"}
_TEXT_FIELDS = {"funds_tuition_currency", "funds_living_currency", "funds_owner_allowed",
                "english_level", "applies_to_city"}
_EXCEPTION = re.compile(r"\b(unless|except|exempt|do not need|if you|when you)\b|豁免|除非|如果", re.I)


def _canonical(value):
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        items = [_canonical(item) for item in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False))
    if isinstance(value, str):
        return " ".join(value.split()).casefold()
    return value


def canonical_policy(policy):
    """No free-text remark; conditions remain business data."""
    return {key: (_canonical(value) if key in _TEXT_FIELDS or key == "conditions"
                  else str(Decimal(str(value)).normalize()) if key in _NUMBER_FIELDS and value is not None
                  else value)
            for key, value in policy.items()
            if key != "remark" and value is not None and (key != "conditions" or value)}


def canonical_documents(documents):
    by_code = {}
    for doc in documents:
        code = doc["code"].strip().casefold()
        value = {"code": code, "required": doc.get("required"),
                 "condition": _canonical(doc.get("condition")) if doc.get("condition") else None}
        if code in by_code and by_code[code] != value:
            raise ValueError(f"材料 {code} 出现相互矛盾的条件")
        by_code[code] = value
    return by_code


@dataclass(frozen=True)
class Comparison:
    result: str  # INITIAL, NO_CHANGE, REVIEW, INCOMPLETE
    changes: tuple[dict, ...]
    issues: tuple[str, ...]


def compare(current_policy, current_documents, proposed_policy, proposed_documents, *, complete):
    old = canonical_policy(current_policy or {})
    new = canonical_policy(proposed_policy)
    old_docs = canonical_documents(current_documents or [])
    new_docs = canonical_documents(proposed_documents)
    changes = []
    issues = []
    if not complete:
        issues.append("页面或章节抽取未完整覆盖；不能据此删改正式知识")
    if proposed_policy.get("remark") and _EXCEPTION.search(proposed_policy["remark"]):
        if not proposed_policy.get("conditions"):
            issues.append("remark 含资格/豁免描述，但缺少结构化 conditions")
    for field in sorted(set(old) | set(new)):
        before, after = old.get(field), new.get(field)
        if before == after:
            continue
        if field in old and field not in new:
            issues.append(f"字段 {field} 本次缺失，不能认定官方删除")
            continue
        changes.append({"kind": "field", "field": field, "before": before, "after": after})
    for code in sorted(set(old_docs) | set(new_docs)):
        before, after = old_docs.get(code), new_docs.get(code)
        if before == after:
            continue
        if before and not after:
            issues.append(f"材料 {code} 本次漏抽或原文删除尚未确认")
            continue
        changes.append({"kind": "document", "code": code, "before": before, "after": after})
    if issues:
        result = "INCOMPLETE"
    elif current_policy is None:
        result = "INITIAL"
    elif changes:
        result = "REVIEW"
    else:
        result = "NO_CHANGE"
    return Comparison(result, tuple(changes), tuple(issues))
