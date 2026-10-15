"""数据审核后台：展示管理库（admin.sqlite3）REVIEW_QUEUE 的待审核项，管理员批准（写入
Agent1 正式库）或驳回（不入库）；处理完的审核项从队列删除。

认证：POST /v1/admin/login {username, password}，对照管理库 ADMIN_USER（PBKDF2 哈希）；
账号用 `agent2.py admin add <用户名>` 创建。
"""
import json
import secrets
import sqlite3
import time

from agent1 import admin_store, formal_pipeline, storage

from .service import ServiceError

DOMAIN_KEYS = {"VISA_POLICY": "policy_id", "FX_POLICY": "policy_id", "ACCOUNT_THRESHOLD": "threshold_id",
               "CREDIT_CARD": "card_id", "SCHOOL": "school_id", "PROGRAM": "program_id",
               "TUITION": "tuition_id"}
APPLICABLE = {"VISA_POLICY", "FX_POLICY", "TUITION"}
SESSION_SECONDS = 8 * 3600
MAX_NOTE = 500


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
    def __init__(self, paths, *, clock=time.monotonic):
        self.paths = paths
        self._clock = clock
        self._sessions = {}

    # ---------- auth ----------

    def _accounts(self, readonly=True):
        if not self.paths.admin_database.exists():
            raise ServiceError("ADMIN_UNAVAILABLE", "管理库不存在，请先运行 agent1 db init", 503)
        return admin_store.session(self.paths, readonly=readonly)

    def login(self, username, password):
        username = username.strip() if isinstance(username, str) else ""
        if not username or not isinstance(password, str) or not password:
            raise ServiceError("INVALID_INPUT", "请填写用户名和密码", 400)
        with self._accounts(readonly=False) as db:
            if not admin_store.authenticate(db, username, password):
                if not admin_store.list_users(db):
                    raise ServiceError("ADMIN_NO_ACCOUNTS",
                                       "尚未创建管理员账号，请先运行 agent2.py admin add <用户名>", 503)
                raise ServiceError("ADMIN_DENIED", "用户名或密码错误", 401)
        token = secrets.token_urlsafe(32)
        self._sessions[token] = (username, self._clock() + SESSION_SECONDS)
        return {"token": token, "admin": username}

    def actor_for_token(self, token):
        entry = self._sessions.get(token) if token else None
        if entry and entry[1] >= self._clock():
            with self._accounts() as db:
                if db.execute("SELECT 1 FROM ADMIN_USER WHERE username=?", (entry[0],)).fetchone():
                    return entry[0]
        self._sessions.pop(token, None)
        raise ServiceError("ADMIN_UNAUTHORIZED", "请先登录审核后台", 401)

    # ---------- review queue ----------

    def _session(self, readonly=True):
        if not self.paths.database.exists():
            raise ServiceError("AGENT1_UNAVAILABLE", "Agent1 数据库不存在", 503)
        return storage.session(self.paths, readonly=readonly)

    def _brief(self, row):
        payload = json.loads(row["proposed_value"] or "{}")
        return {"review_id": row["review_id"], "domain_table": row["domain_table"],
                "record_key": row["record_key"], "action": row["action"], "field_path": row["field_path"],
                "reason": row["reason"], "created_at": row["created_at"],
                "summary": _summary(row["domain_table"], payload),
                "applicable": row["domain_table"] in APPLICABLE}

    def list_reviews(self, domain=None):
        if domain and domain not in DOMAIN_KEYS:
            raise ServiceError("INVALID_INPUT", "未知的域表", 400)
        sql, params = "SELECT * FROM admin.REVIEW_QUEUE", []
        if domain:
            sql, params = sql + " WHERE domain_table=?", [domain]
        with self._session() as db:
            rows = db.execute(sql + " ORDER BY created_at DESC", params).fetchall()
            domains = [r[0] for r in db.execute(
                "SELECT DISTINCT domain_table FROM admin.REVIEW_QUEUE ORDER BY domain_table")]
        return {"reviews": [self._brief(r) for r in rows], "domains": domains}

    def get_review(self, review_id):
        with self._session() as db:
            row = db.execute("SELECT * FROM admin.REVIEW_QUEUE WHERE review_id=?", (review_id,)).fetchone()
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
                if not db.execute("SELECT 1 FROM admin.REVIEW_QUEUE WHERE review_id=?", (review_id,)).fetchone():
                    raise ServiceError("NOT_FOUND", "审核项不存在或已处理", 404)
                return decide(db)
        except ValueError as exc:
            raise ServiceError("REVIEW_CONFLICT", str(exc), 409)
        except sqlite3.IntegrityError as exc:
            raise ServiceError("REVIEW_CONFLICT", f"写入违反数据库约束，未入库：{exc}", 409)

    def approve(self, actor, review_id, note=None):
        note = _note(note, required=False)
        return self._decide(review_id, lambda db: formal_pipeline.apply_review(
            db, review_id, decided_by=actor, note=note))

    def reject(self, actor, review_id, note):
        note = _note(note, required=True)
        return self._decide(review_id, lambda db: formal_pipeline.reject_review(
            db, review_id, decided_by=actor, note=note))
