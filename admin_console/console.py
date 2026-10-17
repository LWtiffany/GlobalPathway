"""AdminConsole: the review-queue management facade (PostgreSQL-backed).

Login -> list/show pending reviews -> approve (write to the domain table) or reject
(drop). Handles sessions in-process (token -> username), 8h lifetime.
"""
import json
import secrets
import time
from pathlib import Path

from . import apply_review, auth, config, db

DOMAIN_KEYS = {"VISA_POLICY": "policy_id", "FX_POLICY": "policy_id", "ACCOUNT_THRESHOLD": "threshold_id",
               "CREDIT_CARD": "card_id", "SCHOOL": "school_id", "PROGRAM": "program_id",
               "TUITION": "tuition_id"}
APPLICABLE = {"VISA_POLICY", "FX_POLICY", "TUITION"}


class ConsoleError(Exception):
    def __init__(self, code, message, http_status=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


def _source_index():
    """sources.json 是网页地址的登记；审核库里不一定存着 URL。"""
    path = Path(__file__).resolve().parent.parent / "sources.json"
    if not path.is_file():
        return {}
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(entries, list):
        return {}
    return {entry["id"]: entry for entry in entries if isinstance(entry, dict) and entry.get("id")}


def _evidence_for_display(raw):
    sources = _source_index()
    shown = []
    for item in json.loads(raw or "[]"):
        if not isinstance(item, dict):
            continue
        row = dict(item)
        source = sources.get(row.get("source_id")) or {}
        row.setdefault("source_url", source.get("url"))
        row.setdefault("source_name", source.get("name"))
        shown.append(row)
    return shown


def _group_key(row, payload):
    """Same universal conflict copied onto several purposes shares one key."""
    if row["domain_table"] != "FX_POLICY":
        return None
    policy = payload.get("policy") or {}
    choices = {}
    for fact in json.loads(row["evidence_json"] or "[]"):
        field = fact.get("field") if isinstance(fact, dict) else None
        if not field:
            continue
        choices.setdefault(field, set()).add(json.dumps(fact.get("value"), ensure_ascii=False, sort_keys=True))
    multi = {field: sorted(values) for field, values in choices.items() if len(values) > 1}
    if not multi:
        return None
    return json.dumps({
        "country": policy.get("country"), "action": row["action"],
        "fields": {field: policy.get(field) for field in multi}, "choices": multi,
        "documents": payload.get("documents") or [],
        "removed": payload.get("removed_documents") or [],
    }, ensure_ascii=False, sort_keys=True)


def _summary(domain, payload):
    shared = payload.get("shared_titles")
    if shared:
        country = (payload.get("policy") or {}).get("country")
        return " · ".join(part for part in (country, "留学购汇：" + "、".join(shared)) if part)
    if domain == "TUITION":
        t = payload.get("tuition") or {}
        amount = t.get("amount")
        money = f"{amount:g} {t.get('currency') or ''}".strip() \
            if isinstance(amount, (int, float)) else "金额未定"
        parts = [payload.get("school_key"), payload.get("program_key"), t.get("academic_year"),
                 t.get("fee_status"), t.get("rate_label"), money]
        return " · ".join(str(p) for p in parts if p)
    policy = payload.get("policy") or {}
    parts = [policy.get("country"), policy.get("title")]
    return " · ".join(str(p) for p in parts if p) or None


def _note(value, required):
    note = (value or "").strip() if isinstance(value, str) or value is None else None
    if note is None or len(note) > config.MAX_NOTE:
        raise ConsoleError("INVALID_INPUT", f"备注须为不超过 {config.MAX_NOTE} 字的文本", 400)
    if required and not note:
        raise ConsoleError("INVALID_INPUT", "驳回必须填写原因", 400)
    return note or None


class AdminConsole:
    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self._sessions = {}

    # ---------- auth ----------

    def login(self, username, password):
        username = username.strip() if isinstance(username, str) else ""
        if not username or not isinstance(password, str) or not password:
            raise ConsoleError("INVALID_INPUT", "请填写用户名和密码", 400)
        connection = db.connect_and_prepare()
        try:
            if not auth.authenticate(connection, username, password):
                if not auth.list_users(connection):
                    raise ConsoleError("ADMIN_NO_ACCOUNTS",
                                       "尚未创建管理员账号，请先运行 admin add <用户名>", 503)
                raise ConsoleError("ADMIN_DENIED", "用户名或密码错误", 401)
            connection.commit()
        finally:
            connection.close()
        token = secrets.token_urlsafe(32)
        self._sessions[token] = (username, self._clock() + config.SESSION_SECONDS)
        return {"token": token, "admin": username}

    def actor_for_token(self, token):
        entry = self._sessions.get(token) if token else None
        if entry and entry[1] >= self._clock():
            connection = db.connect()
            try:
                if connection.execute("SELECT 1 FROM admin.admin_user WHERE username=%s",
                                      (entry[0],)).fetchone():
                    return entry[0]
            finally:
                connection.close()
        self._sessions.pop(token, None)
        raise ConsoleError("ADMIN_UNAUTHORIZED", "请先登录审核后台", 401)

    # ---------- review queue ----------

    def _brief(self, row):
        payload = json.loads(row["proposed_value"] or "{}")
        return {"review_id": row["review_id"], "domain_table": row["domain_table"],
                "record_key": row["record_key"], "action": row["action"], "field_path": row["field_path"],
                "reason": row["reason"], "created_at": row["created_at"],
                "summary": _summary(row["domain_table"], payload),
                "purpose": (payload.get("policy") or {}).get("purpose_code"),
                "group_key": _group_key(row, payload),
                "applicable": row["domain_table"] in APPLICABLE}

    def list_reviews(self, domain=None):
        if domain and domain not in DOMAIN_KEYS:
            raise ConsoleError("INVALID_INPUT", "未知的域表", 400)
        connection = db.connect()
        try:
            sql, params = "SELECT * FROM admin.review_queue", []
            if domain:
                sql, params = sql + " WHERE domain_table=%s", [domain]
            rows = connection.execute(sql + " ORDER BY created_at DESC", params).fetchall()
            domains = [r["domain_table"] for r in connection.execute(
                "SELECT DISTINCT domain_table FROM admin.review_queue ORDER BY domain_table")]
            return {"reviews": [self._brief(r) for r in rows], "domains": domains}
        finally:
            connection.close()

    def get_review(self, review_id):
        connection = db.connect()
        try:
            row = connection.execute("SELECT * FROM admin.review_queue WHERE review_id=%s",
                                     (review_id,)).fetchone()
            if not row:
                raise ConsoleError("NOT_FOUND", "审核项不存在", 404)
            key = DOMAIN_KEYS.get(row["domain_table"])
            current = None
            if key:
                current = connection.execute(f"SELECT * FROM {row['domain_table']} WHERE {key}=%s",
                                             (row["record_key"],)).fetchone()
            return {**self._brief(row), "proposed_value": json.loads(row["proposed_value"] or "{}"),
                    "evidence": _evidence_for_display(row["evidence_json"]),
                    "current": current}
        finally:
            connection.close()

    def _decide(self, review_id, decide):
        connection = db.connect_and_prepare()
        try:
            if not connection.execute("SELECT 1 FROM admin.review_queue WHERE review_id=%s",
                                      (review_id,)).fetchone():
                raise ConsoleError("NOT_FOUND", "审核项不存在或已处理", 404)
            result = decide(connection)
            connection.commit()
            return result
        except ConsoleError:
            connection.rollback()
            raise
        except ValueError as exc:
            connection.rollback()
            raise ConsoleError("REVIEW_CONFLICT", str(exc), 409)
        except Exception as exc:  # constraint violations etc.
            connection.rollback()
            raise ConsoleError("REVIEW_CONFLICT", f"写入失败，未入库：{exc}", 409)
        finally:
            connection.close()

    def approve(self, actor, review_id, note=None, selections=None):
        note = _note(note, required=False)
        if selections is not None and not isinstance(selections, dict):
            raise ConsoleError("INVALID_INPUT", "选定取值须为字段到取值的对象", 400)
        return self._decide(review_id, lambda c: apply_review.apply_review(
            c, review_id, decided_by=actor, note=note, selections=selections))

    def reject(self, actor, review_id, note):
        note = _note(note, required=True)
        return self._decide(review_id, lambda c: apply_review.reject_review(
            c, review_id, decided_by=actor, note=note))
