"""数据审核后台：展示 Agent1 REVIEW_QUEUE，管理员批准（写入正式库）或驳回（不入库）。

认证：POST /v1/admin/login {name, password}，口令取自 AGENT2_ADMIN_PASSWORD
（或 local_secrets.AGENT2_ADMIN_PASSWORD）；未配置时后台整体关闭。
"""
import hmac
import json
import os
import secrets
import sqlite3
import time

from agent1 import formal_pipeline, storage

from .service import ServiceError

STATUSES = ("OPEN", "APPROVED", "REJECTED")
DOMAIN_KEYS = {"VISA_POLICY": "policy_id", "FX_POLICY": "policy_id", "ACCOUNT_THRESHOLD": "threshold_id",
               "CREDIT_CARD": "card_id", "SCHOOL": "school_id", "PROGRAM": "program_id",
               "TUITION": "tuition_id"}
APPLICABLE = {"VISA_POLICY", "FX_POLICY", "TUITION"}
SESSION_SECONDS = 8 * 3600
MAX_NOTE = 500


def configured_password():
    password = os.environ.get("AGENT2_ADMIN_PASSWORD")
    if password:
        return password
    try:
        from local_secrets import AGENT2_ADMIN_PASSWORD
    except ImportError:
        return None
    return AGENT2_ADMIN_PASSWORD or None


def _summary(domain, payload):
    if domain == "TUITION":
        t = payload.get("tuition") or {}
        amount = t.get("amount")
        money = f"{amount:g} {t.get('currency') or ''}".strip() if isinstance(amount, (int, float)) else "金额未定"
        parts = [payload.get("school_key"), payload.get("program_key"), t.get("academic_year"),
                 t.get("fee_status"), t.get("rate_label"), money]
        return " · ".join(str(p) for p in parts if p)
    policy = payload.get("policy") or {}
    parts = [policy.get("country"), policy.get("title")]
    return " · ".join(str(p) for p in parts if p) or None


def _note(value, required):
    note = (value or "").strip() if isinstance(value, str) or value is None else None
    if note is None or len(note) > MAX_NOTE:
        raise ServiceError("INVALID_INPUT", f"备注须为不超过 {MAX_NOTE} 字的文本", 400)
    if required and not note:
        raise ServiceError("INVALID_INPUT", "驳回必须填写原因", 400)
    return note or None


class AdminConsole:
    def __init__(self, paths, password, *, clock=time.monotonic):
        self.paths = paths
        self._password = password
        self._clock = clock
        self._sessions = {}

    # ---------- auth ----------

    def _require_enabled(self):
        if not self._password:
            raise ServiceError("ADMIN_DISABLED", "未配置 AGENT2_ADMIN_PASSWORD，审核后台未启用", 503)

    def login(self, name, password):
        self._require_enabled()
        name = name.strip() if isinstance(name, str) else ""
        if not name or len(name) > 40:
            raise ServiceError("INVALID_INPUT", "请填写审核人姓名（40 字以内）", 400)
        if not isinstance(password, str) or not hmac.compare_digest(
                password.encode("utf-8"), self._password.encode("utf-8")):
            raise ServiceError("ADMIN_DENIED", "口令错误", 401)
        token = secrets.token_urlsafe(32)
        self._sessions[token] = (name, self._clock() + SESSION_SECONDS)
        return {"token": token, "admin": name}

    def actor_for_token(self, token):
        self._require_enabled()
        entry = self._sessions.get(token) if token else None
        if not entry or entry[1] < self._clock():
            self._sessions.pop(token, None)
            raise ServiceError("ADMIN_UNAUTHORIZED", "请先登录审核后台", 401)
        return entry[0]

    # ---------- review queue ----------

    def _session(self, readonly=True):
        if not self.paths.database.exists():
            raise ServiceError("AGENT1_UNAVAILABLE", "Agent1 数据库不存在", 503)
        return storage.session(self.paths, readonly=readonly)

    def _brief(self, row):
        payload = json.loads(row["proposed_value"] or "{}")
        return {"review_id": row["review_id"], "domain_table": row["domain_table"],
                "record_key": row["record_key"], "action": row["action"], "field_path": row["field_path"],
                "reason": row["reason"], "status": row["status"], "created_at": row["created_at"],
                "decided_by": row["decided_by"], "decided_at": row["decided_at"],
                "decision_note": row["decision_note"], "summary": _summary(row["domain_table"], payload),
                "applicable": row["domain_table"] in APPLICABLE}

    def list_reviews(self, status="OPEN", domain=None):
        if status not in STATUSES:
            raise ServiceError("INVALID_INPUT", f"status 须为 {'/'.join(STATUSES)}", 400)
        if domain and domain not in DOMAIN_KEYS:
            raise ServiceError("INVALID_INPUT", "未知的域表", 400)
        where, params = "", []
        if domain:
            where, params = " WHERE domain_table=?", [domain]
        with self._session() as db:
            counts = {s: 0 for s in STATUSES}
            counts.update(dict(db.execute(
                f"SELECT status, COUNT(*) FROM REVIEW_QUEUE{where} GROUP BY status", params).fetchall()))
            clause = " AND ".join(filter(None, [where.removeprefix(" WHERE "), "status=?"]))
            rows = db.execute(f"SELECT * FROM REVIEW_QUEUE WHERE {clause} ORDER BY created_at DESC",
                              [*params, status]).fetchall()
            domains = [r[0] for r in db.execute(
                "SELECT DISTINCT domain_table FROM REVIEW_QUEUE ORDER BY domain_table")]
        return {"reviews": [self._brief(r) for r in rows], "counts": counts, "domains": domains}

    def get_review(self, review_id):
        with self._session() as db:
            row = db.execute("SELECT * FROM REVIEW_QUEUE WHERE review_id=?", (review_id,)).fetchone()
            if not row:
                raise ServiceError("NOT_FOUND", "审核项不存在", 404)
            key = DOMAIN_KEYS.get(row["domain_table"])
            current = db.execute(f"SELECT * FROM {row['domain_table']} WHERE {key}=?",
                                 (row["record_key"],)).fetchone() if key else None
        return {**self._brief(row), "proposed_value": json.loads(row["proposed_value"] or "{}"),
                "evidence": json.loads(row["evidence_json"] or "[]"),
                "current": dict(current) if current else None}

    def _decide(self, review_id, decide):
        try:
            with self._session(readonly=False) as db:
                if not db.execute("SELECT 1 FROM REVIEW_QUEUE WHERE review_id=?", (review_id,)).fetchone():
                    raise ServiceError("NOT_FOUND", "审核项不存在", 404)
                decide(db)
        except ValueError as exc:
            raise ServiceError("REVIEW_CONFLICT", str(exc), 409)
        except sqlite3.IntegrityError as exc:
            raise ServiceError("REVIEW_CONFLICT", f"写入违反数据库约束，未入库：{exc}", 409)
        return self.get_review(review_id)

    def approve(self, actor, review_id, note=None):
        note = _note(note, required=False)
        return self._decide(review_id, lambda db: formal_pipeline.apply_review(
            db, review_id, decided_by=f"admin:{actor}", note=note))

    def reject(self, actor, review_id, note):
        note = _note(note, required=True)
        return self._decide(review_id, lambda db: formal_pipeline.reject_review(
            db, review_id, decided_by=f"admin:{actor}", note=note))
