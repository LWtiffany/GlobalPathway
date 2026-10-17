"""审核队列给业务人员看的说明，不使用字段代号和内部编号。"""
import json

FIELD_LABELS = {
    "annual_quota_amount": "年度购汇额度",
    "annual_quota_currency": "额度币种",
    "quota_reset": "额度重置方式",
    "over_quota_allowed": "超过年度额度是否仍可购汇",
    "tuition_direct_to_school": "学费是否直接汇给学校",
    "remitter_relation": "代办人关系",
    "can_use_family_quota": "是否可以使用家人的购汇额度",
    "effective_from": "生效日期",
    "effective_to": "截止日期",
    "cas_required": "是否需要 CAS",
    "i20_required": "是否需要 I-20",
    "funds_tuition_unpaid": "未缴学费证明金额",
    "funds_tuition_currency": "学费证明币种",
    "funds_tuition_required": "是否要求学费资金证明",
    "funds_living_monthly": "每月生活费",
    "funds_living_currency": "生活费币种",
    "funds_living_months": "生活费需覆盖月数",
    "funds_holding_days": "资金需持有天数",
    "funds_doc_max_age_days": "资金证明有效天数",
    "funds_owner_allowed": "允许的资金持有人",
    "english_level": "英语要求",
    "tb_test_required": "是否需要肺结核检测",
    "health_surcharge": "医疗附加费",
    "apply_window_days": "最早可提前申请天数",
    "processing_days": "审理时长（天）",
    "visa_fee": "签证费",
    "applies_to_city": "适用城市",
    "amount": "金额",
    "currency": "币种",
}

VALUE_LABELS = {
    "immediate_family": "直系亲属（父母、子女、配偶）",
    "close_relatives": "近亲属",
    "anyone_authorized": "经授权的他人",
    "self_only": "仅限本人",
    "calendar_year": "自然年",
    "tuition": "学费",
    "living": "生活费",
}

BOOL_FIELDS = {
    "over_quota_allowed", "tuition_direct_to_school", "can_use_family_quota",
    "cas_required", "i20_required", "tb_test_required", "funds_tuition_required",
}


def field_label(field):
    return FIELD_LABELS.get(field, str(field))


def value_label(field, value):
    if isinstance(value, (list, tuple)):
        return "、".join(value_label(field, item) for item in value)
    if field in BOOL_FIELDS and value in (0, 1, True, False):
        return "是" if value in (1, True) else "否"
    if isinstance(value, str) and value in VALUE_LABELS:
        return VALUE_LABELS[value]
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def conflict_sentence(field, values):
    shown = value_label(field, list(values))
    return (f"不同来源对「{field_label(field)}」的说法不一致：{shown}。"
            "请对照原文网页确认采用哪一种。")


def change_sentence(changed):
    parts = [f"「{field_label(field)}」由 {value_label(field, before)} 改为 {value_label(field, after)}"
             for field, (before, after) in changed.items()]
    return "与已入库内容不同：" + "；".join(parts)


def missing_currency_sentence():
    return "写了年度购汇额度，但没有同时写明币种，无法直接入库。"


def tuition_conflict_sentence(values):
    shown = "、".join(str(value) for value in values)
    return f"不同页面给出的学费金额不一致：{shown}。请对照原文网页确认采用哪一个。"


def stamp_source_urls(evidence, sources):
    """Copy the registered page URL onto each evidence item for the review screen."""
    if not isinstance(evidence, list) or not isinstance(sources, dict):
        return evidence
    stamped = []
    for item in evidence:
        if not isinstance(item, dict):
            stamped.append(item)
            continue
        row = dict(item)
        source = sources.get(row.get("source_id")) or {}
        if source.get("url"):
            row["source_url"] = source["url"]
        if source.get("name"):
            row["source_name"] = source["name"]
        stamped.append(row)
    return stamped
