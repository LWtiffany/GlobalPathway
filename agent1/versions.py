"""知识版本、变化识别与受控发布（设计 4.3-5/6、4.5、A1.4、A1.6、ADR-002）。

路由规则：
- 门控 error            -> 隔离（版本 REJECTED，不发布）；
- 内容未变化            -> 不产生新版本，仅刷新最近成功核验时间；
- 版式/噪声变化        -> 不产生新版本，记录 change_type；
- 低风险且门控全通过    -> 自动发布；
- 高风险 / 关键变更 / 跨源冲突 / 门控 warning -> PENDING_REVIEW 转人工。
"""
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone

from . import config, db as db_mod, gate as gate_mod


def policy_key(source_id, schema_type, applies_to_city):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{source_id}:{schema_type}:{applies_to_city or '*'}"))


def _canonical(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def _hash(obj):
    return hashlib.sha256(_canonical(obj).encode()).hexdigest()


# 不参与"业务内容是否变化"判定的自由文本/易波动字段（模型措辞每次不同，但语义无变化）。
# 这些字段的变化视为噪声，不触发 auto_update；仍会随 payload 一并保存，但不影响 business_hash。
NOISE_FIELDS = {"remark"}


def business_content(source, patch):
    """从抽取结果提炼业务内容与业务指纹。

    返回 (business_dict, business_hash, content_hash, entries, documents)
    entries: [{policy_key, applies_to_city, policy(非空字段), evidence}]
    业务指纹只含业务字段值，且排除 remark 等自由文本噪声字段，用于区分
    真实内容变化与措辞波动；content_hash 则含全部字段（含 remark 与证据），
    用于区分"完全没变"与"措辞变了但业务值没变"。
    """
    source_id = source["source_id"]
    schema_type = source.get("schema_type", "generic_facts")
    if schema_type == "visa_policy":
        entries = []
        for entry in patch.policies:
            policy = entry.policy.model_dump(exclude_none=True)
            city = policy.get("applies_to_city")
            entries.append({
                "policy_key": policy_key(source_id, schema_type, city),
                "applies_to_city": city,
                "policy": policy,
                "evidence": [ev.model_dump() for ev in entry.evidence],
            })
        documents = [d.model_dump() for d in patch.documents]
        # business 用于变化判定：剔除噪声字段，避免 remark 措辞波动触发误更新
        business = {
            "policies": [{"applies_to_city": e["applies_to_city"],
                          "policy": {k: v for k, v in e["policy"].items() if k not in NOISE_FIELDS}}
                         for e in entries],
            "documents": sorted(d["code"] for d in documents),
        }
        payload = {"policies": entries, "documents": documents}
    else:
        facts = [f.model_dump() for f in patch.facts]
        entries = [{
            "policy_key": policy_key(source_id, schema_type, None),
            "applies_to_city": None,
            "policy": {"facts": sorted(
                [{"topic": f["topic"], "value": f["value"]} for f in facts],
                key=lambda x: x["topic"])},
            "evidence": facts,
        }]
        business = {"facts": entries[0]["policy"]["facts"]}
        payload = {"facts": facts}
    return business, _hash(business), _hash(payload), entries, payload


def _field_is_high_risk(schema_type, changed_field):
    if schema_type == "visa_policy":
        return True  # 签证政策字段整体按高风险治理
    return gate_mod.is_high_risk_text(changed_field)


def _diff_fields(latest_business, business, schema_type):
    """比较业务字段，返回 (policy_changes, document_changes) 两类变化。

    policy_changes：数值/布尔等结构化字段变化（如 'London.funds_living_monthly'），
                    这些字段抽取稳定，可自动更新；
    document_changes：文档清单的增删（如 'document:cas'），语义判断不稳定，
                      需人工确认（转人工审核）。
    """
    policy_changes = []
    document_changes = []
    if schema_type == "visa_policy":
        old = {(p.get("applies_to_city") or "*"): p.get("policy", {})
               for p in (latest_business or {}).get("policies", [])}
        new = {(p.get("applies_to_city") or "*"): p.get("policy", {})
               for p in business.get("policies", [])}
        for scope in sorted(set(old) | set(new)):
            before, after = old.get(scope, {}), new.get(scope, {})
            for field in sorted(set(before) | set(after)):
                if before.get(field) != after.get(field):
                    policy_changes.append(f"{scope}.{field}")
        old_docs = set((latest_business or {}).get("documents", []))
        new_docs = set(business.get("documents", []))
        for code in sorted(old_docs ^ new_docs):
            document_changes.append(f"document:{code}")
    else:
        old = {f["topic"]: f["value"] for f in (latest_business or {}).get("facts", [])}
        new = {f["topic"]: f["value"] for f in business.get("facts", [])}
        for topic in sorted(set(old) | set(new)):
            if old.get(topic) != new.get(topic):
                policy_changes.append(f"facts:{topic}")
    return policy_changes, document_changes


def _detect_conflicts(db, source, business):
    """跨来源冲突：同 Schema 同适用范围下，不同官方来源取值不一致（设计 4.4）。"""
    conflicts = []
    schema_type = source.get("schema_type", "generic_facts")
    if schema_type != "visa_policy":
        return conflicts
    mine = {(p.get("applies_to_city") or "*"): p.get("policy", {}) for p in business.get("policies", [])}
    rows = db.execute(
        "SELECT * FROM knowledge_versions WHERE schema_type=? AND source_id<>? "
        "AND status IN ('PUBLISHED','PENDING_REVIEW','APPROVED')",
        (schema_type, source["source_id"])).fetchall()
    for row in rows:
        other = json.loads(row["payload_json"])
        for entry in other.get("policies", []):
            scope = entry.get("applies_to_city") or "*"
            if scope not in mine:
                continue
            for field, value in entry.get("policy", {}).items():
                if field in mine[scope] and mine[scope][field] != value:
                    conflicts.append({
                        "field": f"{scope}.{field}", "kept": mine[scope][field],
                        "other_source": row["source_id"], "other_value": value,
                    })
    return conflicts


def process(db, source, snapshot, patch, meta, report, run_id):
    """抽取+门控通过后的版本处理与路由。返回 outcome dict。

    策略（2026-10-12 依据用户需求调整）：来源为「每日 0 点抓取上一天更新」的低频
    场景，字段值小幅变化是常态。因此：
    - 门控 error -> 仍隔离（不发布，写 problems.json）；
    - 内容未变化 -> 不产生新版本，仅刷新核验时间；
    - 内容有变化 -> **原地更新当前发布版本**（不新建版本、不送人工审核），
      记审计日志留痕；无已发布版本时（首次）才新建版本并走审核/发布。
    """
    source_id = source["source_id"]
    schema_type = source.get("schema_type", "generic_facts")
    business, business_hash, content_hash, entries, payload = business_content(source, patch)
    latest = db_mod.latest_version(db, source_id)
    # 与 business_content 的 business 对齐：剔除 remark 等噪声字段，避免措辞波动误判为变化
    latest_business = json.loads(json.dumps(
        {"policies": [{"applies_to_city": e.get("applies_to_city"),
                       "policy": {k: v for k, v in (e.get("policy") or {}).items()
                                  if k not in NOISE_FIELDS}}
                      for e in json.loads(latest["payload_json"]).get("policies", [])],
         "documents": sorted(d["code"] for d in json.loads(latest["payload_json"]).get("documents", []))}
    )) if latest and schema_type == "visa_policy" else (
        {"facts": json.loads(latest["payload_json"]).get("facts", [])} if latest else None)

    policy_keys = [{"policy_key": e["policy_key"], "applies_to_city": e["applies_to_city"]} for e in entries]
    payload_json = json.dumps(payload, ensure_ascii=False)

    # 门控 error：隔离
    if report.errors:
        version_id = db_mod.insert_version(
            db,
            source_id=source_id, schema_type=schema_type, snapshot_hash=snapshot["page_hash"],
            business_hash=business_hash, content_hash=content_hash,
            version_no=db_mod.next_version_no(db, source_id),
            supersedes=latest["version_id"] if latest else None,
            payload_json=payload_json, policy_keys_json=json.dumps(policy_keys),
            risk_level="high", change_type="isolated",
            changed_fields_json=json.dumps([p.get("field") for p in report.errors if p.get("field")]),
            key_change=1, status="REJECTED", verified_at=None,
            model=meta.get("model"), schema_version=meta.get("schema_version"),
            prompt_version=meta.get("prompt_version"), run_id=run_id,
            conflicts_json=json.dumps([]),
        )
        db_mod.audit(db, "agent1", "gate_isolate", "knowledge_version", version_id,
                     {"errors": len(report.errors), "run_id": run_id})
        db_mod.add_event(db, "knowledge.isolated", source_id, version_id, {"run_id": run_id})
        return {"result": "isolated", "version_id": version_id,
                "reason": f"{len(report.errors)} 个门控 error，已隔离并写入 problems.json"}

    # 内容未变化 / 仅版式噪声：不产生新版本，刷新核验时间。
    # 判定基准是"当前发布版本"：撤回/回滚后指针变化时必须重新处理，不能短路。
    pointer = db_mod.published_row(db, source_id)
    if pointer:
        current = db_mod.get_version(db, pointer["version_id"])
        if current and current["business_hash"] == business_hash:
            change_type = "none" if current["content_hash"] == content_hash else "noise"
            db_mod.touch_verified(db, current["version_id"])
            db_mod.audit(db, "agent1", "no_change", "knowledge_version", current["version_id"],
                         {"change_type": change_type, "run_id": run_id})
            return {"result": "no_change", "version_id": current["version_id"],
                    "change_type": change_type}

    policy_changes, document_changes = _diff_fields(latest_business, business, schema_type)
    conflicts = _detect_conflicts(db, source, business)

    # 有已发布版本：
    # - 仅策略字段变化（数值等稳定字段）-> 原地自动更新当前版本；
    # - 有文档清单变化（语义判断不稳定）-> 转人工审核，不自动合并。
    if pointer:
        current = db_mod.get_version(db, pointer["version_id"])
        if document_changes:
            # 文档清单变化：转人工，保留当前发布版本不动，另建待审版本
            review_id = db_mod.add_review(
                db, current["version_id"], source_id, "document_change", "high")
            db_mod.audit(db, "agent1", "document_change_review", "knowledge_version",
                         current["version_id"],
                         {"document_changes": document_changes,
                          "policy_changes": policy_changes, "run_id": run_id})
            db_mod.add_event(db, "knowledge.document_change", source_id, current["version_id"],
                             {"document_changes": document_changes, "review_id": review_id})
            return {"result": "pending_review", "version_id": current["version_id"],
                    "review_id": review_id, "reason": "document_change",
                    "document_changes": document_changes,
                    "changed_fields": policy_changes,
                    "reason": "文档清单有变化，需人工确认后再合并"}
        change_type = "content" if policy_changes else "noise"
        db_mod.update_version_in_place(
            db, current["version_id"],
            snapshot_hash=snapshot["page_hash"], business_hash=business_hash,
            content_hash=content_hash, payload_json=payload_json,
            policy_keys_json=json.dumps(policy_keys),
            changed_fields_json=json.dumps(policy_changes, ensure_ascii=False),
            run_id=run_id,
        )
        db_mod.touch_verified(db, current["version_id"])
        db_mod.replace_index_rows(db, source_id, schema_type, current["version_id"],
                                  policy_keys, db_mod.now_iso())
        db_mod.audit(db, "agent1", "auto_update", "knowledge_version", current["version_id"],
                     {"changed_fields": policy_changes, "run_id": run_id})
        db_mod.add_event(db, "knowledge.auto_updated", source_id, current["version_id"],
                         {"changed_fields": policy_changes, "run_id": run_id})
        return {"result": "updated", "version_id": current["version_id"],
                "changed_fields": policy_changes,
                "reason": "内容变化已原地更新当前版本（不新建版本）"}

    # 首次（无任何版本）或撤回到无发布指针：走原有新建版本 + 高风险送审/发布流程
    change_type = "initial" if not latest else "content"
    needs_review = bool(report.warnings) or report.risk_level == "high" or conflicts
    status = "PENDING_REVIEW" if needs_review else "APPROVED"
    if conflicts:
        reason = "source_conflict"
    elif report.risk_level == "high":
        reason = "high_risk_new"
    else:
        reason = "gate_warning"

    version_id = db_mod.insert_version(
        db,
        source_id=source_id, schema_type=schema_type, snapshot_hash=snapshot["page_hash"],
        business_hash=business_hash, content_hash=content_hash,
        version_no=db_mod.next_version_no(db, source_id),
        supersedes=latest["version_id"] if latest else None,
        payload_json=payload_json, policy_keys_json=json.dumps(policy_keys),
        risk_level=report.risk_level, change_type=change_type,
        changed_fields_json=json.dumps(policy_changes, ensure_ascii=False),
        key_change=0, status=status, verified_at=db_mod.now_iso(),
        model=meta.get("model"), schema_version=meta.get("schema_version"),
        prompt_version=meta.get("prompt_version"), run_id=run_id,
        conflicts_json=json.dumps(conflicts, ensure_ascii=False),
    )

    if status == "PENDING_REVIEW":
        review_id = db_mod.add_review(db, version_id, source_id, reason, report.risk_level)
        db_mod.audit(db, "agent1", "review_required", "knowledge_version", version_id,
                     {"reason": reason, "changed_fields": policy_changes, "conflicts": conflicts})
        db_mod.add_event(db, "knowledge.review_required", source_id, version_id,
                         {"reason": reason, "review_id": review_id})
        return {"result": "pending_review", "version_id": version_id, "review_id": review_id,
                "reason": reason, "changed_fields": policy_changes}

    publish_version(db, version_id, actor="agent1:auto", action="auto_publish")
    return {"result": "published", "version_id": version_id, "changed_fields": policy_changes}


def publish_version(db, version_id, actor, action="publish"):
    """发布服务：事务提交版本状态、当前指针、索引、审计与事件（设计 4.5）。"""
    version = db_mod.get_version(db, version_id)
    if not version:
        raise ValueError(f"版本不存在：{version_id}")
    if version["status"] not in ("PENDING_REVIEW", "APPROVED", "WITHDRAWN"):
        raise ValueError(f"版本状态 {version['status']} 不允许发布")
    payload = json.loads(version["payload_json"])
    policy_keys = json.loads(version["policy_keys_json"])
    db_mod.set_version_status(db, version_id, "PUBLISHED")
    db_mod.set_published_pointer(db, version["source_id"], version_id, actor)
    db_mod.replace_index_rows(db, version["source_id"], version["schema_type"], version_id,
                              policy_keys, version["verified_at"])
    db_mod.audit(db, actor, action, "knowledge_version", version_id,
                 {"source_id": version["source_id"], "version_no": version["version_no"]})
    db_mod.add_event(db, "knowledge.published", version["source_id"], version_id,
                     {"actor": actor, "action": action})
    return version_id


def resolve_withdraw_target(db, target):
    version = db_mod.get_version(db, target)
    if version:
        return version
    published = db_mod.published_row(db, target)
    if published:
        return db_mod.get_version(db, published["version_id"])
    raise ValueError(f"找不到可撤回的版本：{target}（既不是 version_id 也没有已发布版本）")


def withdraw(db, target, reason, actor):
    """紧急撤回：立即失效并强制理由、审计与事件（设计 4.5/9.4）。"""
    if not reason or not reason.strip():
        raise ValueError("撤回必须填写理由")
    version = resolve_withdraw_target(db, target)
    db_mod.set_version_status(db, version["version_id"], "WITHDRAWN")
    db_mod.clear_published_pointer(db, version["source_id"])
    db_mod.set_index_availability(db, version["source_id"], "withdrawn")
    db_mod.audit(db, actor, "withdraw", "knowledge_version", version["version_id"],
                 {"reason": reason, "source_id": version["source_id"]})
    db_mod.add_event(db, "knowledge.withdrawn", version["source_id"], version["version_id"],
                     {"reason": reason, "actor": actor})
    return version["version_id"]


def rollback(db, source_id, to_version_id, reason, actor, force=False):
    """回滚到旧版本：核对旧版本仍满足新鲜度后才恢复指针（设计 4.5）。"""
    if not reason or not reason.strip():
        raise ValueError("回滚必须填写理由")
    version = db_mod.get_version(db, to_version_id)
    if not version or version["source_id"] != source_id:
        raise ValueError(f"版本 {to_version_id} 不属于来源 {source_id}")
    source = db_mod.get_source(db, source_id)
    sla_hours = source.get("freshness_sla_hours") or 168
    verified = db_mod.parse_ts(version.get("verified_at"))
    if verified and datetime.now(timezone.utc) - verified > timedelta(hours=sla_hours) and not force:
        raise ValueError(f"旧版本已超过新鲜度阈值（{sla_hours}h），确认仍要回滚请使用 --force")
    policy_keys = json.loads(version["policy_keys_json"])
    db_mod.set_version_status(db, version["version_id"], "PUBLISHED")
    db_mod.set_published_pointer(db, source_id, version["version_id"], actor)
    db_mod.replace_index_rows(db, source_id, version["schema_type"], version["version_id"],
                              policy_keys, version.get("verified_at"))
    db_mod.audit(db, actor, "rollback", "knowledge_version", version["version_id"],
                 {"reason": reason, "source_id": source_id})
    db_mod.add_event(db, "knowledge.rolled_back", source_id, version["version_id"],
                     {"reason": reason, "actor": actor})
    return version["version_id"]


def decide_review(db, review_id, decision, actor, note):
    """审核决策：批准即发布；退回即否决该版本。两者都结算关联门控问题。"""
    review = db_mod.get_review(db, review_id)
    if not review:
        raise ValueError(f"审核任务不存在：{review_id}")
    if review["status"] != "OPEN":
        raise ValueError(f"审核任务已处理：{review_id}")
    version = db_mod.get_version(db, review["version_id"])
    if decision == "approve":
        publish_version(db, review["version_id"], actor, action="review_approve_publish")
    elif decision == "reject":
        db_mod.set_version_status(db, review["version_id"], "REJECTED")
    else:
        raise ValueError("decision 必须是 approve 或 reject")
    db_mod.decide_review(db, review_id, decision, actor, note)
    db_mod.audit(db, actor, f"review_{decision}", "review", review_id,
                 {"version_id": review["version_id"], "note": note})
    gate_mod.resolve_problems(version_id=review["version_id"],
                              resolution=f"review {decision} by {actor}")
    return review["version_id"]
