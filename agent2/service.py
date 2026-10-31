"""S1 业务服务：M01 身份授权、M02/M03 档案与提案、M04 会话、M11 Advice。

所有读写都按 user_id 做对象级授权；他人的资源与不存在的资源返回同一个 NOT_FOUND，不泄露内容。
"""
import asyncio
import json
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from . import clarify
from .models import (AnswerResult, BankRelationship, BankRelationshipProposal, Profile,
                     ProfileChangeProposal, ProfileField)
from .intent import classify
from .llm import build_llm_components
from .llm.validation import validate_understanding
from .orchestration import build_default_workflow
from .orchestration.contracts import OrchestrationInput
from .orchestration.legacy_adapter import (V2_INTENTS, card_summaries_from_steps,
                                            intents_for_confirmed_fields,
                                            intents_for_session_topic, run_v2,
                                            session_topic_for_intents)
from .orchestration.planner import RuleTaskPlanner
from .passwords import (dummy_hash, hash_password, needs_rehash, password_length_error,
                        password_length_ok, verify_password)
from .runner import Agent2Runner
from .store import dumps

DEFAULT_PROFILE_FIELDS = ("role", "destination_country", "visa_category", "school", "planned_start_date")
SESSION_TTL = timedelta(days=7)
SESSION_COOKIE = "gp_session"
_DUMMY_PASSWORD_HASH = None


class ServiceError(Exception):
    def __init__(self, code, message, http_status, retryable=False):
        super().__init__(message)
        self.code, self.message, self.http_status, self.retryable = code, message, http_status, retryable


def not_found(kind):
    return ServiceError("NOT_FOUND", f"{kind}不存在", 404)


def bad_request(message):
    return ServiceError("INVALID_INPUT", message, 400)


def _new_id():
    return str(uuid.uuid4())


def utc_now():
    return datetime.now(timezone.utc)


class Agent2Service:
    def __init__(self, store, knowledge, *, clock=utc_now, generator=None, demo_enabled=True, suggest=None,
                 credit_card_repository=None, bank_account_repository=None, llm_components=None,
                 accounts=None):
        self.store = store
        self.knowledge = knowledge
        self.clock = clock
        self.demo_enabled = demo_enabled
        self.accounts = accounts
        self.runner = Agent2Runner(knowledge, clock, generator=generator, suggest=suggest)
        llm = llm_components if llm_components is not None else build_llm_components()
        self.v2_workflow = build_default_workflow(
            credit_card_repository=credit_card_repository,
            bank_repository=bank_account_repository,
            understanding=llm.understanding,
            writer=llm.writer,
        )
        self._sync_field_definitions()

    def _sync_field_definitions(self):
        now = self._ts()
        with self.store.transaction() as db:
            for code, item in clarify.field_definitions().items():
                db.execute("""INSERT INTO profile_field_definitions
                    (field_code,display_name,value_type,options_json,domains_json,sensitivity,freshness_days,
                     ask_priority,ask_only_when_needed,storage_class,confirmation_mode,scope,
                     question_template,schema_version,active,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,1,?)
                    ON CONFLICT(field_code) DO UPDATE SET display_name=excluded.display_name,
                    value_type=excluded.value_type, options_json=excluded.options_json,
                    domains_json=excluded.domains_json, sensitivity=excluded.sensitivity,
                    freshness_days=excluded.freshness_days, ask_priority=excluded.ask_priority,
                    ask_only_when_needed=excluded.ask_only_when_needed,
                    storage_class=excluded.storage_class,
                    confirmation_mode=excluded.confirmation_mode,scope=excluded.scope,
                    question_template=excluded.question_template, schema_version=excluded.schema_version,
                    active=excluded.active, updated_at=excluded.updated_at""",
                           (code, item["label"], item["kind"], dumps(item["options"]),
                            dumps(item["domains"]), item["sensitivity"], item["freshness_days"],
                            item["ask_priority"], int(item["ask_only_when_needed"]),
                            item["storage_class"], item["confirmation_mode"], item["scope"],
                            item["question"], now))

    def _ts(self):
        return self.clock().isoformat()

    @staticmethod
    def _token_hash(token):
        import hashlib
        return hashlib.sha256((token or "").encode("utf-8")).hexdigest()

    def _create_session(self, db, user_id):
        raw = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        now = self.clock()
        expires = now + SESSION_TTL
        db.execute(
            "INSERT INTO auth_sessions (token_hash, user_id, csrf_token, created_at, expires_at) VALUES (?,?,?,?,?)",
            (self._token_hash(raw), user_id, csrf, now.isoformat(), expires.isoformat()))
        return {"token": raw, "csrf_token": csrf, "expires_at": expires.isoformat()}

    def _ensure_local_user(self, db, *, user_id, subject, display_name, synthetic):
        row = db.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row:
            db.execute("UPDATE users SET display_name=?, external_subject=?, synthetic=? WHERE user_id=?",
                       (display_name, subject, int(synthetic), user_id))
            return user_id
        by_subject = db.execute("SELECT user_id FROM users WHERE external_subject=?", (subject,)).fetchone()
        if by_subject:
            # Keep existing local row if subject already mapped (demo collisions).
            return by_subject["user_id"]
        db.execute(
            "INSERT INTO users (user_id, external_subject, display_name, synthetic) VALUES (?,?,?,?)",
            (user_id, subject, display_name, int(synthetic)))
        return user_id

    # ---------- M01 ----------

    def demo_login(self, name):
        if not self.demo_enabled:
            raise ServiceError("DEMO_DISABLED", "当前环境未开启模拟登录", 403)
        if not isinstance(name, str) or not name.strip():
            raise bad_request("需要模拟用户名")
        subject = f"demo:{name.strip()}"
        with self.store.transaction() as db:
            row = db.execute("SELECT user_id FROM users WHERE external_subject=?", (subject,)).fetchone()
            user_id = row["user_id"] if row else _new_id()
            if not row:
                db.execute("INSERT INTO users (user_id, external_subject, display_name, synthetic) VALUES (?,?,?,1)",
                           (user_id, subject, name.strip()))
            session = self._create_session(db, user_id)
        return {"token": session["token"], "csrf_token": session["csrf_token"],
                "user_id": user_id, "synthetic": True,
                "notice": "模拟身份，仅用于 Demo，不是真实银行账户登录。",
                "_set_session": session["token"]}

    def login(self, username, password):
        global _DUMMY_PASSWORD_HASH
        if self.accounts is None:
            raise ServiceError("LOGIN_UNAVAILABLE", "账号登录未配置", 503)
        if not isinstance(username, str) or not username.strip():
            raise bad_request("需要用户名")
        if not isinstance(password, str) or not password:
            raise bad_request("需要密码")
        if not password_length_ok(password):
            raise bad_request(password_length_error())
        username = username.strip()
        row = self.accounts.get_by_username(username)
        if _DUMMY_PASSWORD_HASH is None:
            _DUMMY_PASSWORD_HASH = dummy_hash()
        if not row:
            verify_password(password, _DUMMY_PASSWORD_HASH)
            raise ServiceError("INVALID_CREDENTIALS", "用户名或密码错误", 401)
        locked_until = row.get("locked_until")
        if locked_until is not None:
            if isinstance(locked_until, (int, float)):
                locked_dt = datetime.fromtimestamp(locked_until, tz=timezone.utc)
            else:
                locked_dt = locked_until if locked_until.tzinfo else locked_until.replace(tzinfo=timezone.utc)
            if locked_dt > self.clock():
                raise ServiceError("ACCOUNT_LOCKED", "登录失败次数过多，请稍后再试", 423)
        if row.get("status") != "active":
            raise ServiceError("ACCOUNT_DISABLED", "账号不可用", 403)
        if not verify_password(password, row["password_hash"]):
            self.accounts.record_failure(row["user_id"])
            raise ServiceError("INVALID_CREDENTIALS", "用户名或密码错误", 401)
        self.accounts.clear_failures(row["user_id"])
        if needs_rehash(row["password_hash"]):
            self.accounts.update_password_hash(row["user_id"], hash_password(password))
        subject = row.get("external_subject") or f"rds:{row['user_id']}"
        display = row["username"]
        with self.store.transaction() as db:
            local_id = self._ensure_local_user(
                db, user_id=row["user_id"], subject=subject, display_name=display, synthetic=False)
            user_id = local_id or row["user_id"]
            session = self._create_session(db, user_id)
        return {"user_id": user_id, "csrf_token": session["csrf_token"], "synthetic": False,
                "_set_session": session["token"]}

    def logout(self, token):
        if not token:
            return {"ok": True, "_clear_session": True}
        with self.store.transaction() as db:
            db.execute("DELETE FROM auth_sessions WHERE token_hash=?", (self._token_hash(token),))
        return {"ok": True, "_clear_session": True}

    def session_for_token(self, token):
        if not token:
            raise ServiceError("UNAUTHENTICATED", "需要登录", 401)
        row = self.store.one(
            "SELECT user_id, csrf_token, expires_at FROM auth_sessions WHERE token_hash=?",
            (self._token_hash(token),))
        if not row:
            raise ServiceError("UNAUTHENTICATED", "需要登录", 401)
        expires = datetime.fromisoformat(row["expires_at"])
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= self.clock():
            with self.store.transaction() as db:
                db.execute("DELETE FROM auth_sessions WHERE token_hash=?", (self._token_hash(token),))
            raise ServiceError("UNAUTHENTICATED", "登录已过期", 401)
        return row

    def user_for_token(self, token):
        return self.session_for_token(token)["user_id"]

    def me(self, user_id):
        user = self.store.one("SELECT user_id, display_name, synthetic FROM users WHERE user_id=?", (user_id,))
        if not user:
            raise ServiceError("UNAUTHENTICATED", "需要登录", 401)
        user["synthetic"] = bool(user["synthetic"])
        return user

    # ---------- M02 ----------

    def _owned_profile_row(self, user_id, profile_id):
        row = self.store.one("SELECT * FROM profiles WHERE profile_id=? AND user_id=?", (profile_id, user_id))
        if not row:
            raise not_found("档案")
        return row

    def get_profile(self, user_id, profile_id):
        row = self._owned_profile_row(user_id, profile_id)
        fields = {}
        for r in self.store.all("SELECT * FROM profile_field_values WHERE profile_id=?", (profile_id,)):
            status = r["status"]
            if status == "confirmed" and r["expires_at"]:
                expires = datetime.fromisoformat(r["expires_at"])
                if expires <= self.clock():
                    status = "stale"
            fields[r["field_code"]] = ProfileField(
                value=json.loads(r["value_json"]) if r["value_json"] else None,
                status=status, source_type=r["source_type"], confirmed_at=r["confirmed_at"],
                expires_at=r["expires_at"], source_message_id=r["source_message_id"],
                field_version=r["field_version"], sensitivity=r["sensitivity"])
        return Profile(profile_id=profile_id, user_id=user_id, version=row["version"],
                       display_name=row["display_name"], status=row["status"],
                       synthetic=bool(row["synthetic"]), fields=fields)

    def list_profiles(self, user_id):
        rows = self.store.all("SELECT profile_id FROM profiles WHERE user_id=? ORDER BY created_at, rowid",
                              (user_id,))
        return {"profiles": [self.get_profile(user_id, r["profile_id"]) for r in rows]}

    @staticmethod
    def _check_values(values):
        if not isinstance(values, dict):
            raise bad_request("fields 必须是对象")
        for code, value in values.items():
            error = clarify.validate(code, value)
            if error:
                raise bad_request(f"{clarify.label(code)}：{error}")

    @staticmethod
    def _check_profile_values(values):
        Agent2Service._check_values(values)
        transient = [code for code in values
                     if clarify.persistence_policy(code)["storage_class"]
                     not in {"core_profile", "preference"}]
        if transient:
            labels = "、".join(clarify.label(code) for code in transient)
            raise bad_request(f"{labels}属于本次咨询或账户记录，不能写入长期资料")

    @staticmethod
    def _value_type(value):
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, list):
            return "multi_enum"
        if isinstance(value, (int, float)):
            return "number"
        if isinstance(value, dict):
            return "object"
        return "string"

    def _write_fields(self, db, profile_id, values, source_type, *, source_message_id=None,
                      profile_version=None, actor_type="user"):
        now = self._ts()
        for code, value in values.items():
            old = db.execute("SELECT value_json, field_version FROM profile_field_values "
                             "WHERE profile_id=? AND field_code=?", (profile_id, code)).fetchone()
            definition = db.execute("SELECT sensitivity, freshness_days FROM profile_field_definitions "
                                    "WHERE field_code=?", (code,)).fetchone()
            field_version = (old["field_version"] if old else 0) + 1
            sensitivity = definition["sensitivity"] if definition else "standard"
            expires_at = (self.clock() + timedelta(days=definition["freshness_days"])).isoformat() \
                if definition and definition["freshness_days"] is not None else None
            db.execute("""INSERT INTO profile_field_values
                (profile_id, field_code, value_json, value_type, status, source_type, confirmed_at,
                 expires_at, source_message_id, field_version, sensitivity, updated_at)
                VALUES (?,?,?,?, 'confirmed', ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile_id, field_code) DO UPDATE SET value_json=excluded.value_json,
                value_type=excluded.value_type, status='confirmed', source_type=excluded.source_type,
                confirmed_at=excluded.confirmed_at, expires_at=excluded.expires_at,
                source_message_id=excluded.source_message_id, field_version=excluded.field_version,
                sensitivity=excluded.sensitivity, updated_at=excluded.updated_at""",
                       (profile_id, code, dumps(value), self._value_type(value), source_type, now,
                        expires_at, source_message_id, field_version, sensitivity, now))
            db.execute("""INSERT INTO profile_field_events
                (event_id, profile_id, field_code, old_value_json, new_value_json, action, source_type,
                 source_message_id, actor_type, profile_version, created_at)
                VALUES (?,?,?,?,?,'set',?,?,?,?,?)""",
                       (_new_id(), profile_id, code, old["value_json"] if old else None, dumps(value),
                        source_type, source_message_id, actor_type, profile_version or 1, now))

    def create_profile(self, user_id, fields=None, *, synthetic=False, display_name=None):
        values = fields or {}
        self._check_profile_values(values)
        if display_name is not None and (not isinstance(display_name, str) or not display_name.strip()):
            raise bad_request("display_name 必须是非空文本")
        profile_id = _new_id()
        now = self._ts()
        with self.store.transaction() as db:
            db.execute("""INSERT INTO profiles
                (profile_id,user_id,display_name,version,status,synthetic,created_at,updated_at)
                VALUES (?,?,?,1,'active',?,?,?)""",
                       (profile_id, user_id, display_name.strip() if display_name else None,
                        int(synthetic), now, now))
            for code in DEFAULT_PROFILE_FIELDS:
                db.execute("""INSERT INTO profile_field_values
                    (profile_id,field_code,value_json,value_type,status,source_type,confirmed_at,expires_at,
                     source_message_id,field_version,sensitivity,updated_at)
                    VALUES (?,?,NULL,'json','unknown',NULL,NULL,NULL,NULL,0,'standard',?)""",
                           (profile_id, code, now))
            self._write_fields(db, profile_id, values, "demo" if synthetic else "user_edit",
                               profile_version=1)
        return self.get_profile(user_id, profile_id)

    def patch_profile(self, user_id, profile_id, *, expected_version, set_fields=None, clear_fields=()):
        set_fields = set_fields or {}
        self._check_profile_values(set_fields)
        unknown = [c for c in clear_fields if c not in clarify.FIELDS]
        if unknown:
            raise bad_request(f"未知字段：{', '.join(unknown)}")
        overlap = set(set_fields) & set(clear_fields)
        if overlap:
            raise bad_request(f"同一字段不能同时设置和清空：{', '.join(sorted(overlap))}")
        with self.store.transaction() as db:
            row = db.execute("SELECT version FROM profiles WHERE profile_id=? AND user_id=?",
                             (profile_id, user_id)).fetchone()
            if not row:
                raise not_found("档案")
            self._bump(db, profile_id, row["version"], expected_version)
            self._write_fields(db, profile_id, set_fields, "user_edit", profile_version=row["version"] + 1)
            for code in clear_fields:
                old = db.execute("SELECT value_json FROM profile_field_values WHERE profile_id=? AND field_code=?",
                                 (profile_id, code)).fetchone()
                now = self._ts()
                db.execute("""INSERT INTO profile_field_values
                    (profile_id,field_code,value_json,value_type,status,source_type,confirmed_at,expires_at,
                     source_message_id,field_version,sensitivity,updated_at)
                    VALUES (?,?,NULL,'json','unknown',NULL,NULL,NULL,NULL,0,'standard',?)
                    ON CONFLICT(profile_id, field_code) DO UPDATE SET value_json=NULL, status='unknown',
                    source_type=NULL, confirmed_at=NULL, expires_at=NULL, source_message_id=NULL,
                    field_version=profile_field_values.field_version+1, updated_at=excluded.updated_at""",
                           (profile_id, code, now))
                db.execute("""INSERT INTO profile_field_events
                    (event_id,profile_id,field_code,old_value_json,new_value_json,action,source_type,
                     source_message_id,actor_type,profile_version,created_at)
                    VALUES (?,?,?,?,NULL,'clear','user_edit',NULL,'user',?,?)""",
                           (_new_id(), profile_id, code, old["value_json"] if old else None,
                            row["version"] + 1, now))
        return self.get_profile(user_id, profile_id)

    def _bump(self, db, profile_id, current, expected):
        if expected != current:
            raise ServiceError("VERSION_CONFLICT", f"档案已更新到 v{current}，请刷新后再提交", 409)
        db.execute("UPDATE profiles SET version=version+1, updated_at=? WHERE profile_id=?", (self._ts(), profile_id))

    # ---------- M03 ----------

    def list_proposals(self, user_id, profile_id, status="open"):
        self._owned_profile_row(user_id, profile_id)
        rows = self.store.all("SELECT * FROM profile_change_proposals WHERE profile_id=? AND status=? "
                              "ORDER BY created_at", (profile_id, status))
        return [self._proposal(r) for r in rows]

    @staticmethod
    def _proposal(row):
        return ProfileChangeProposal(
            proposal_id=row["proposal_id"], profile_id=row["profile_id"], field_code=row["field_code"],
            old_value=json.loads(row["old_value"]) if row["old_value"] else None,
            new_value=json.loads(row["new_value"]) if row["new_value"] else None,
            source_message_id=row["source_message_id"], basis=row["basis"], status=row["status"])

    def _owned_proposal(self, user_id, proposal_id):
        row = self.store.one("""SELECT p.* FROM profile_change_proposals p JOIN profiles f USING(profile_id)
            WHERE p.proposal_id=? AND f.user_id=?""", (proposal_id, user_id))
        if not row:
            raise not_found("档案变更提案")
        return row

    def confirm_proposal(self, user_id, proposal_id, *, expected_version):
        row = self._owned_proposal(user_id, proposal_id)
        if row["status"] != "open":
            raise ServiceError("PROPOSAL_CLOSED", "该提案已处理", 409)
        with self.store.transaction() as db:
            current = db.execute("SELECT version FROM profiles WHERE profile_id=?", (row["profile_id"],)).fetchone()
            self._bump(db, row["profile_id"], current["version"], expected_version)
            self._write_fields(db, row["profile_id"], {row["field_code"]: json.loads(row["new_value"])},
                               "user_message", source_message_id=row["source_message_id"],
                               profile_version=current["version"] + 1)
            db.execute("UPDATE profile_change_proposals SET status='confirmed', decided_at=? WHERE proposal_id=?",
                       (self._ts(), proposal_id))
        return self.get_profile(user_id, row["profile_id"])

    def confirm_proposals(self, user_id, proposal_ids, *, expected_version):
        """Confirm one message's Profile changes atomically with one version bump."""
        if not isinstance(proposal_ids, list) or not proposal_ids:
            raise bad_request("需要至少一项资料更新提议")
        proposal_ids = list(dict.fromkeys(proposal_ids))
        placeholders = ",".join("?" for _ in proposal_ids)
        rows = self.store.all(f"""SELECT p.* FROM profile_change_proposals p
            JOIN profiles f USING(profile_id)
            WHERE p.proposal_id IN ({placeholders}) AND f.user_id=?""",
                              (*proposal_ids, user_id))
        if len(rows) != len(proposal_ids):
            raise not_found("档案变更提案")
        if any(row["status"] != "open" for row in rows):
            raise ServiceError("PROPOSAL_CLOSED", "其中一项提议已处理", 409)
        profile_ids = {row["profile_id"] for row in rows}
        if len(profile_ids) != 1:
            raise bad_request("一次只能确认同一份资料的更新")
        profile_id = next(iter(profile_ids))
        # If the same field somehow appears twice, the latest proposal wins.
        rows.sort(key=lambda row: row["created_at"])
        values = {row["field_code"]: json.loads(row["new_value"]) for row in rows}
        now = self._ts()
        with self.store.transaction() as db:
            current = db.execute("SELECT version FROM profiles WHERE profile_id=?",
                                 (profile_id,)).fetchone()
            self._bump(db, profile_id, current["version"], expected_version)
            self._write_fields(db, profile_id, values, "user_message",
                               source_message_id=rows[-1]["source_message_id"],
                               profile_version=current["version"] + 1)
            db.executemany("""UPDATE profile_change_proposals
                SET status='confirmed', decided_at=? WHERE proposal_id=?""",
                           [(now, row["proposal_id"]) for row in rows])
            db.execute("UPDATE sessions SET profile_version=? WHERE profile_id=?",
                       (current["version"] + 1, profile_id))
        return self.get_profile(user_id, profile_id)

    def dismiss_proposal(self, user_id, proposal_id):
        self._owned_proposal(user_id, proposal_id)
        with self.store.transaction() as db:
            db.execute("UPDATE profile_change_proposals SET status='dismissed', decided_at=? "
                       "WHERE proposal_id=? AND status='open'", (self._ts(), proposal_id))
        return {"proposal_id": proposal_id, "status": "dismissed"}

    # ---------- Bank relationships (multi-market child records) ----------

    @staticmethod
    def _relationship(row):
        return BankRelationship(
            relationship_id=row["relationship_id"], profile_id=row["profile_id"],
            institution=row["institution"], market=row["market"],
            product_id=row["product_id"], product_name=row["product_name_snapshot"],
            holder_relation=row["holder_relation"], status=row["status"],
            opened_on=row["opened_on"], source_message_id=row["source_message_id"],
            confirmed_at=row["confirmed_at"], last_confirmed_at=row["last_confirmed_at"],
            version=row["version"],
        )

    @staticmethod
    def _relationship_proposal(row):
        return BankRelationshipProposal(
            proposal_id=row["proposal_id"], profile_id=row["profile_id"],
            market=row["market"], product_id=row["product_id"],
            product_name=row["product_name_snapshot"],
            holder_relation=row["holder_relation"], proposed_status=row["proposed_status"],
            existing_relationship_id=row["existing_relationship_id"],
            source_message_id=row["source_message_id"], basis=row["basis"], status=row["status"],
        )

    def list_bank_relationships(self, user_id, profile_id):
        self._owned_profile_row(user_id, profile_id)
        rows = self.store.all("""SELECT * FROM bank_relationships WHERE profile_id=?
            ORDER BY CASE market WHEN 'CN-HK' THEN 1 WHEN 'UK' THEN 2 WHEN 'US' THEN 3 ELSE 4 END,
                     created_at""", (profile_id,))
        return {"profile_id": profile_id,
                "relationships": [self._relationship(row) for row in rows]}

    def list_bank_relationship_proposals(self, user_id, profile_id):
        self._owned_profile_row(user_id, profile_id)
        rows = self.store.all("""SELECT * FROM bank_relationship_proposals
            WHERE profile_id=? AND status='open' ORDER BY created_at""", (profile_id,))
        return {"profile_id": profile_id,
                "proposals": [self._relationship_proposal(row) for row in rows]}

    def confirm_bank_relationship_proposal(self, user_id, proposal_id, *, expected_version,
                                           proposed_status=None):
        row = self.store.one("""SELECT p.* FROM bank_relationship_proposals p
            JOIN profiles f USING(profile_id) WHERE p.proposal_id=? AND f.user_id=?""",
                             (proposal_id, user_id))
        if not row:
            raise not_found("银行账户更新提议")
        if row["status"] != "open":
            raise ServiceError("PROPOSAL_CLOSED", "该提议已处理", 409)
        relationship_status = proposed_status or row["proposed_status"]
        if relationship_status not in {"selected", "applying", "open", "blocked", "closed"}:
            raise bad_request("账户状态无效")
        now = self._ts()
        with self.store.transaction() as db:
            profile_row = db.execute("SELECT version FROM profiles WHERE profile_id=?",
                                     (row["profile_id"],)).fetchone()
            self._bump(db, row["profile_id"], profile_row["version"], expected_version)
            relationship_id = row["existing_relationship_id"]
            existing = db.execute("SELECT * FROM bank_relationships WHERE relationship_id=?",
                                  (relationship_id,)).fetchone() if relationship_id else None
            if not existing:
                existing = db.execute("""SELECT * FROM bank_relationships
                    WHERE profile_id=? AND market=? AND COALESCE(product_id,'')=COALESCE(?, '')
                      AND holder_relation=? AND status!='closed' ORDER BY updated_at DESC LIMIT 1""",
                    (row["profile_id"], row["market"], row["product_id"],
                     row["holder_relation"])).fetchone()
            old_status = existing["status"] if existing else None
            if existing:
                relationship_id = existing["relationship_id"]
                db.execute("""UPDATE bank_relationships SET status=?, product_name_snapshot=?,
                    source_message_id=?, last_confirmed_at=?, version=version+1, updated_at=?
                    WHERE relationship_id=?""",
                    (relationship_status, row["product_name_snapshot"],
                     row["source_message_id"], now, now, relationship_id))
            else:
                relationship_id = _new_id()
                db.execute("""INSERT INTO bank_relationships
                    (relationship_id,profile_id,institution,market,product_id,product_name_snapshot,
                     holder_relation,status,opened_on,source_message_id,confirmed_at,last_confirmed_at,
                     version,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,NULL,?,?,?,?,?,?)""",
                    (relationship_id, row["profile_id"], "HSBC", row["market"], row["product_id"],
                     row["product_name_snapshot"], row["holder_relation"], relationship_status,
                     row["source_message_id"], now, now, 1, now, now))
            db.execute("INSERT INTO bank_relationship_events VALUES (?,?,?,?,?,?,?)",
                       (_new_id(), relationship_id, old_status, relationship_status,
                        row["source_message_id"], "user", now))
            db.execute("UPDATE bank_relationship_proposals SET status='confirmed', decided_at=? WHERE proposal_id=?",
                       (now, proposal_id))
            db.execute("UPDATE sessions SET profile_version=? WHERE profile_id=?",
                       (profile_row["version"] + 1, row["profile_id"]))
        relationship = self.store.one("SELECT * FROM bank_relationships WHERE relationship_id=?",
                                      (relationship_id,))
        return {"relationship": self._relationship(relationship),
                "profile_version": profile_row["version"] + 1}

    def dismiss_bank_relationship_proposal(self, user_id, proposal_id):
        row = self.store.one("""SELECT p.proposal_id FROM bank_relationship_proposals p
            JOIN profiles f USING(profile_id) WHERE p.proposal_id=? AND f.user_id=?""",
                             (proposal_id, user_id))
        if not row:
            raise not_found("银行账户更新提议")
        with self.store.transaction() as db:
            db.execute("""UPDATE bank_relationship_proposals SET status='dismissed', decided_at=?
                WHERE proposal_id=? AND status='open'""", (self._ts(), proposal_id))
        return {"proposal_id": proposal_id, "status": "dismissed"}

    # ---------- M04 ----------

    def create_conversation(self, user_id, profile_id):
        profile = self._owned_profile_row(user_id, profile_id)
        existing = self.store.one("SELECT session_id, profile_id, profile_version FROM sessions "
                                  "WHERE user_id=? AND profile_id=?", (user_id, profile_id))
        if existing:
            return {"session_id": existing["session_id"], "conversation_id": existing["session_id"],
                    "profile_id": existing["profile_id"],
                    "profile_version": existing["profile_version"]}
        conversation_id = _new_id()
        with self.store.transaction() as db:
            db.execute("INSERT INTO sessions VALUES (?,?,?,?,?)",
                       (conversation_id, user_id, profile_id, profile["version"], self._ts()))
        return {"session_id": conversation_id, "conversation_id": conversation_id, "profile_id": profile_id,
                "profile_version": profile["version"]}

    def profile_conversation(self, user_id, profile_id):
        """每个档案一条主会话：已有则返回最早那条，否则新建。"""
        self._owned_profile_row(user_id, profile_id)
        row = self.store.one("SELECT session_id AS conversation_id, profile_id, profile_version FROM sessions "
                             "WHERE user_id=? AND profile_id=? ORDER BY created_at LIMIT 1", (user_id, profile_id))
        if row:
            row["session_id"] = row["conversation_id"]
            return row
        return self.create_conversation(user_id, profile_id)

    def list_conversations(self, user_id):
        rows = self.store.all("SELECT session_id, profile_id, profile_version, created_at "
                              "FROM sessions WHERE user_id=? ORDER BY created_at", (user_id,))
        for row in rows:
            row["conversation_id"] = row["session_id"]
        return rows

    def _owned_conversation(self, user_id, conversation_id):
        row = self.store.one("SELECT session_id AS conversation_id, user_id, profile_id, profile_version, created_at "
                             "FROM sessions WHERE session_id=? AND user_id=?",
                             (conversation_id, user_id))
        if not row:
            raise not_found("会话")
        return row

    def list_messages(self, user_id, conversation_id, *, after_seq=0, limit=50):
        self._owned_conversation(user_id, conversation_id)
        rows = self.store.all("""SELECT message_id, seq, role, client_message_id, reply_to, body_json, status,
            created_at FROM message_index WHERE session_id=? AND seq>? ORDER BY seq LIMIT ?""",
                              (conversation_id, after_seq, min(max(int(limit), 1), 200)))
        for row in rows:
            row["body"] = json.loads(row.pop("body_json"))
        return {"messages": rows, "next_after_seq": rows[-1]["seq"] if rows else after_seq}

    @staticmethod
    def _asked_from_assistant_body(body):
        """Fields the client may confirm: follow-ups plus material 待确认 pending_fields."""
        fields = [q["field"] for q in body.get("follow_up_questions") or [] if q.get("field")]
        for material in ((body.get("answer") or {}).get("materials") or []):
            fields.extend(material.get("pending_fields") or [])
        return tuple(dict.fromkeys(code for code in fields if code))

    def _conversation_context(self, conversation_id):
        facts, topic, asked, last_version = {}, None, (), None
        active_intents: list[str] = []
        last_user_text, last_assistant_text = "", ""
        for row in self.store.all("SELECT role, body_json FROM message_index WHERE session_id=? ORDER BY seq",
                                  (conversation_id,)):
            body = json.loads(row["body_json"])
            if row["role"] == "user":
                facts.update(body.get("facts") or {})
                topic = body.get("topic") or topic
                if body.get("active_intents"):
                    active_intents = list(body["active_intents"])
                last_user_text = (body.get("text") or "")[:120]
            else:
                asked = self._asked_from_assistant_body(body)
                last_version = body.get("profile_version", last_version)
                summary = (body.get("answer") or {}).get("summary") or ""
                if summary:
                    last_assistant_text = summary[:300]
        return facts, topic, asked, last_version, active_intents, last_user_text, last_assistant_text

    def _stored_reply(self, conversation_id, client_message_id):
        # Multi-intent turns store several assistant bubbles on the same user
        # message; return the last one (follow-ups / proposals live there).
        row = self.store.one("""SELECT a.body_json FROM message_index u JOIN message_index a
            ON a.reply_to=u.message_id
            WHERE u.session_id=? AND u.role='user' AND u.client_message_id=?
              AND a.role='assistant'
            ORDER BY a.seq DESC LIMIT 1""",
                             (conversation_id, client_message_id))
        return AnswerResult.model_validate_json(row["body_json"]) if row else None

    def _domain_state(self, conversation_id, domain):
        row = self.store.one(
            "SELECT state_json FROM domain_conversation_state WHERE session_id=? AND domain=?",
            (conversation_id, domain),
        )
        return json.loads(row["state_json"]) if row else {}

    def _apply_understanding(self, request: OrchestrationInput):
        """Merge LLM intents/facts into the routing request (rules ∪ model). Fail soft."""
        port = self.v2_workflow.dependencies.understanding
        if (port is None or request.input_mode != "free_text" or not request.text.strip()
                or request.understanding_applied):
            return request, False
        try:
            catalog = clarify.field_catalog()
            raw = asyncio.run(port.understand(request, catalog))
            facts, basis, intents, topic_switch = validate_understanding(
                raw, request, set(catalog), clarify.validate)
            enriched = request.model_copy(update={
                "conversation_intents": list(dict.fromkeys([
                    *request.conversation_intents, *intents])),
                "llm_intents": list(dict.fromkeys(intents)),
                "understood_facts": facts,
                "understood_basis": basis,
                "llm_topic_switch": topic_switch,
                "understanding_applied": True,
            })
            return enriched, True
        except Exception:
            return request, False

    def post_message(self, user_id, conversation_id, text, client_message_id, *, confirmed_fields=None,
                     session_fields=None, expected_profile_version=None, cleared_fields=None):
        conversation = self._owned_conversation(user_id, conversation_id)
        if not isinstance(client_message_id, str) or not client_message_id.strip():
            raise bad_request("需要 client_message_id（幂等键）")
        stored = self._stored_reply(conversation_id, client_message_id)
        if stored:
            return stored
        facts, topic, asked, last_version, active_intents, last_user_text, last_assistant_text = \
            self._conversation_context(conversation_id)
        user_message_id, answer_id = _new_id(), _new_id()
        confirmed_fields = confirmed_fields or {}
        session_fields = session_fields or {}
        cleared_fields = list(dict.fromkeys(cleared_fields or []))
        if confirmed_fields:
            if not isinstance(confirmed_fields, dict) or not set(confirmed_fields) <= set(asked):
                raise bad_request("只能保存当前追问要求的画像字段")
            self._check_values(confirmed_fields)
            # Backward-compatible safety: older clients submitted every chip as
            # confirmed_fields.  Query scope fields are never durable Profile
            # facts, even when sent by such a client.
            transient = {code: value for code, value in confirmed_fields.items()
                         if clarify.persistence_policy(code)["storage_class"] == "session_only"}
            if transient:
                session_fields = {**session_fields, **transient}
                confirmed_fields = {code: value for code, value in confirmed_fields.items()
                                    if code not in transient}
        if session_fields:
            if not isinstance(session_fields, dict) or not set(session_fields) <= set(asked):
                raise bad_request("只能提交当前追问要求的临时字段")
            self._check_values(session_fields)
            facts.update(session_fields)
        unknown_clear = [code for code in cleared_fields if code not in clarify.FIELDS]
        if unknown_clear:
            raise bad_request(f"未知字段：{', '.join(unknown_clear)}")
        # Undo path: drop retracted answers from session facts so the same question
        # is not skipped because an earlier user turn still carries the value.
        for code in cleared_fields:
            facts.pop(code, None)
        profile = self.get_profile(user_id, conversation["profile_id"])
        if confirmed_fields:
            if expected_profile_version != profile.version:
                raise ServiceError("VERSION_CONFLICT", f"档案已更新到 v{profile.version}，请刷新后再提交", 409)
            profile = profile.model_copy(deep=True)
            profile.version += 1
            for code, value in confirmed_fields.items():
                profile.fields[code] = ProfileField(value=value, status="confirmed",
                    source_type="user_selection", confirmed_at=self._ts(), source_message_id=user_message_id,
                    field_version=(profile.fields.get(code).field_version if profile.fields.get(code) else 0) + 1,
                    sensitivity=clarify.field_definitions()[code]["sensitivity"])
        elif cleared_fields:
            profile = profile.model_copy(deep=True)
            profile.version += 1
            for code in cleared_fields:
                old = profile.fields.get(code)
                profile.fields[code] = ProfileField(
                    value=None, status="unknown",
                    field_version=(old.field_version if old else 0) + 1,
                    sensitivity=clarify.field_definitions()[code]["sensitivity"])
        classified = classify(text, asked_fields=asked)
        understood_facts, understood_basis = {}, {}
        understanding_applied = False
        llm_intents: list[str] = []
        llm_topic_switch = False
        # Structured follow-ups continue the open multi-domain turn, but a chip
        # answer is scoped to the domain(s) that own the confirmed field(s) so
        # confirming want_credit_card does not re-emit bank-account advice.
        followup_intents = (list(active_intents) if active_intents
                            else intents_for_session_topic(topic, facts))
        # A pending follow-up must not swallow a clearly new question: when the
        # free text matches another domain's intent (e.g. asking about account
        # recommendations while a credit-card follow-up is open), route to the
        # new question instead of treating it as a follow-up answer.
        topic_intents = set(intents_for_session_topic(topic, facts)) if topic else set()
        field_answers = {**session_fields, **confirmed_fields}
        switches_domain = (
            asked and topic and not field_answers
            and classified.intent not in ("follow_up", "other")
            and classified.intent not in followup_intents
            and classified.intent not in topic_intents
        )
        if asked and topic and not switches_domain:
            resolved_topic = topic
            planned_intents = list(followup_intents)
            if field_answers:
                planned_intents = intents_for_confirmed_fields(
                    field_answers, planned_intents)
        else:
            route_request = OrchestrationInput(
                text=text,
                profile_id=profile.profile_id,
                profile_version=profile.version,
                asked_fields=list(asked),
                conversation_intents=(
                    list(active_intents) if active_intents
                    else intents_for_session_topic(topic, facts) if topic else []),
                session_topic=topic,
                session_last_user_text=last_user_text,
                session_last_assistant_text=last_assistant_text,
                session_pending_questions=list(asked),
            )
            route_request, understanding_applied = self._apply_understanding(route_request)
            understood_facts = dict(route_request.understood_facts)
            understood_basis = dict(route_request.understood_basis)
            llm_intents = list(route_request.llm_intents)
            llm_topic_switch = route_request.llm_topic_switch
            plan = asyncio.run(RuleTaskPlanner().plan(route_request))
            planned_intents = [task.intent for task in plan.tasks]
            session_topic = session_topic_for_intents(planned_intents)
            if plan.routing_audit and plan.routing_audit.refusal_reason == "ambiguous_card":
                resolved_topic = "other"
            elif session_topic:
                resolved_topic = session_topic
            elif classified.intent in ("follow_up", "other") and topic:
                resolved_topic = topic
            else:
                resolved_topic = classified.intent
        use_v2 = (
            resolved_topic in {"credit_card", "remittance", "account", "out_of_scope", "other"}
            or any(intent in V2_INTENTS for intent in planned_intents)
        )
        if use_v2:
            v2_intents = planned_intents or intents_for_session_topic(resolved_topic, facts)
            domain_state = {}
            if "hsbc_account" in v2_intents:
                domain_state["bank_account"] = self._domain_state(
                    conversation_id, "bank_account")
            output = run_v2(self.v2_workflow, profile=profile, text=text,
                            conversation_facts=facts, asked_fields=asked,
                            message_id=answer_id,
                            structured_selection=bool(field_answers),
                            conversation_intents=v2_intents,
                            understood_facts=understood_facts,
                            understood_basis=understood_basis,
                            understanding_applied=understanding_applied,
                            llm_intents=llm_intents,
                            llm_topic_switch=llm_topic_switch,
                            session_topic=topic,
                            session_last_user_text=last_user_text,
                            session_last_assistant_text=last_assistant_text,
                            session_pending_questions=list(asked),
                            domain_state=domain_state)
        else:
            output = self.runner.run(profile=profile, text=text, conversation_facts=facts, conversation_topic=topic,
                                     asked_fields=asked,
                                     expected_profile_version=last_version or conversation["profile_version"],
                                     message_id=answer_id)
        answer = output.result
        if field_answers:
            equivalent = {"applicant_age_band": "applicant_age",
                          "annual_income_band_hkd": "annual_income_hkd"}
            for code in field_answers:
                output.turn_facts.pop(code, None)
                output.turn_basis.pop(code, None)
                alternate = equivalent.get(code)
                if alternate:
                    output.turn_facts.pop(alternate, None)
                    output.turn_basis.pop(alternate, None)
        output.turn_facts.update(session_fields)
        output.turn_basis.update({code: "用户回答本轮追问" for code in session_fields})
        # A market mention in an account-only question describes where the
        # account is being discussed, not automatically where the student will
        # study.  The shared parser may emit both keys, so remove the study fact
        # unless the user actually used study-context language.
        active_output_intents = set(output.active_intents or [])
        account_only = bool(active_output_intents) and active_output_intents <= {"hsbc_account"}
        study_context = bool(re.search(r"留学|上学|读书|就读|学校|去.{0,6}(?:读|念)", text))
        if account_only and not study_context:
            output.turn_facts.pop("destination_country", None)
            output.turn_basis.pop("destination_country", None)
        proposals = self._proposals_for(profile, output, user_message_id)
        relationship_proposals = self._bank_relationship_proposals_for(
            profile, output, user_message_id, text)
        answer.profile_change_proposals = proposals
        answer.bank_relationship_proposals = relationship_proposals
        parts = list(output.message_parts or [answer])
        if parts:
            parts[-1].profile_change_proposals = proposals
            parts[-1].bank_relationship_proposals = relationship_proposals
            output.message_parts = parts
            output.result = parts[-1]
            answer = parts[-1]
        try:
            with self.store.transaction() as db:
                self._persist(db, conversation_id, profile, text, client_message_id, user_message_id, output,
                              confirmed_fields=confirmed_fields, session_fields=session_fields,
                              cleared_fields=cleared_fields)
        except sqlite3.IntegrityError:
            stored = self._stored_reply(conversation_id, client_message_id)
            if stored:
                return stored
            raise
        return answer

    def _proposals_for(self, profile, output, user_message_id):
        proposals = []
        open_rows = self.store.all("SELECT field_code, new_value FROM profile_change_proposals "
                                   "WHERE profile_id=? AND status='open'", (profile.profile_id,))
        already = {(r["field_code"], r["new_value"]) for r in open_rows}
        destination = profile.confirmed("destination_country")
        role = profile.confirmed("role")
        # Only registered profile fields become user-facing proposals (skip session keys like fx_mode).
        for code, value in output.turn_facts.items():
            if code not in clarify.FIELDS:
                continue
            policy = clarify.persistence_policy(code)
            if policy["storage_class"] not in {"core_profile", "preference"} or \
                    policy["confirmation_mode"] == "never":
                continue
            # A free-text request such as “想办信用卡” is a turn-routing intent,
            # not a durable profile change. Persist this field only when the user
            # explicitly answers the structured consent question via confirmed_fields.
            if code == "want_credit_card":
                continue
            if profile.confirmed(code) == value or (code, dumps(value)) in already:
                continue
            # Skip mirrors of already-confirmed profile facts — they are not new choices.
            if code in {"application_region", "account_region"} and destination and value == destination:
                continue
            if code == "applicant_identity" and role and value == role:
                continue
            proposals.append(ProfileChangeProposal(
                proposal_id=_new_id(), profile_id=profile.profile_id, field_code=code,
                old_value=profile.confirmed(code), new_value=value, source_message_id=user_message_id,
                basis=f"用户消息：「{output.turn_basis.get(code, '')}」"))
        return proposals

    def _bank_relationship_proposals_for(self, profile, output, user_message_id, text):
        """Turn an explicit account decision/status statement into a confirmable child-record update."""
        proposals = []
        for item in output.advice_items or []:
            if item.get("topic") != "account":
                continue
            domain = item.get("payload", {}).get("domain_payload") or {}
            state = domain.get("conversation_state") or {}
            act = domain.get("dialogue_act")
            product_id = state.get("selected_product_id") or state.get("focused_product_id")
            status = None
            if re.search(r"已经开好|已经开了|开户成功|已开立|已经有(?:这个|该)?账户", text):
                status = "open"
            elif re.search(r"正在申请|已经申请|已经提交|申请中|等审批|等批核", text):
                status = "applying"
            elif re.search(r"申请失败|开户失败|被拒|补件|补材料", text):
                status = "blocked"
            elif re.search(r"已经关户|已经注销|注销了|关闭了?账户", text):
                status = "closed"
            elif act == "confirm_selection":
                status = "selected"
            if not status:
                continue
            candidates = domain.get("candidate_results") or []
            if not product_id and len(candidates) == 1:
                product_id = candidates[0].get("product", {}).get("account_id")
            product = next((row.get("product", {}) for row in candidates
                            if row.get("product", {}).get("account_id") == product_id), {})
            product_name = product.get("product_name") or product.get("name_zh")
            market = domain.get("account_region") or state.get("market")
            if not market or not product_id or not product_name:
                continue
            holder = output.turn_facts.get("account_applicant_relation") or "profile_subject"
            existing = self.store.one("""SELECT relationship_id FROM bank_relationships
                WHERE profile_id=? AND market=? AND COALESCE(product_id,'')=COALESCE(?, '')
                  AND holder_relation=? AND status!='closed' ORDER BY updated_at DESC LIMIT 1""",
                                      (profile.profile_id, market, product_id, holder))
            duplicate = self.store.one("""SELECT proposal_id FROM bank_relationship_proposals
                WHERE profile_id=? AND market=? AND COALESCE(product_id,'')=COALESCE(?, '')
                  AND holder_relation=? AND proposed_status=? AND status='open' LIMIT 1""",
                                       (profile.profile_id, market, product_id, holder, status))
            if duplicate:
                continue
            proposals.append(BankRelationshipProposal(
                proposal_id=_new_id(), profile_id=profile.profile_id, market=market,
                product_id=product_id, product_name=product_name, holder_relation=holder,
                proposed_status=status,
                existing_relationship_id=existing["relationship_id"] if existing else None,
                source_message_id=user_message_id,
                basis=f"用户消息：「{text.strip()}」",
            ))
        return proposals

    def _persist(self, db, conversation_id, profile, text, client_message_id, user_message_id, output,
                 *, confirmed_fields=None, session_fields=None, cleared_fields=None):
        answer = output.result
        now = self._ts()
        confirmed_fields = confirmed_fields or {}
        cleared_fields = cleared_fields or []
        if confirmed_fields:
            current = db.execute("SELECT version FROM profiles WHERE profile_id=?", (profile.profile_id,)).fetchone()
            expected = profile.version - 1
            self._bump(db, profile.profile_id, current["version"], expected)
            self._write_fields(db, profile.profile_id, confirmed_fields, "user_selection",
                               source_message_id=user_message_id, profile_version=profile.version)
            db.execute("UPDATE sessions SET profile_version=? WHERE session_id=?",
                       (profile.version, conversation_id))
        elif cleared_fields:
            current = db.execute("SELECT version FROM profiles WHERE profile_id=?", (profile.profile_id,)).fetchone()
            new_version = current["version"] + 1
            self._bump(db, profile.profile_id, current["version"], current["version"])
            for code in cleared_fields:
                old = db.execute("SELECT value_json FROM profile_field_values WHERE profile_id=? AND field_code=?",
                                 (profile.profile_id, code)).fetchone()
                db.execute("""INSERT INTO profile_field_values
                    (profile_id,field_code,value_json,value_type,status,source_type,confirmed_at,expires_at,
                     source_message_id,field_version,sensitivity,updated_at)
                    VALUES (?,?,NULL,'json','unknown',NULL,NULL,NULL,NULL,0,'standard',?)
                    ON CONFLICT(profile_id, field_code) DO UPDATE SET value_json=NULL, status='unknown',
                    source_type=NULL, confirmed_at=NULL, expires_at=NULL, source_message_id=NULL,
                    field_version=profile_field_values.field_version+1, updated_at=excluded.updated_at""",
                           (profile.profile_id, code, now))
                db.execute("""INSERT INTO profile_field_events
                    (event_id,profile_id,field_code,old_value_json,new_value_json,action,source_type,
                     source_message_id,actor_type,profile_version,created_at)
                    VALUES (?,?,?,?,NULL,'clear','user_edit',NULL,'user',?,?)""",
                           (_new_id(), profile.profile_id, code, old["value_json"] if old else None,
                            new_version, now))
            db.execute("UPDATE sessions SET profile_version=? WHERE session_id=?",
                       (new_version, conversation_id))
        seq = db.execute("SELECT COALESCE(MAX(seq),0) FROM message_index WHERE session_id=?",
                         (conversation_id,)).fetchone()[0]
        # Keep undone fields out of the stored turn facts so later turns do not
        # revive them from conversation history.
        stored_facts = {code: value for code, value in output.turn_facts.items()
                        if code not in cleared_fields}
        active = list(output.active_intents or [])
        user_body = {"text": text, "facts": stored_facts, "topic": output.topic,
                     "active_intents": active}
        db.execute("INSERT INTO message_index VALUES (?,?,?,?,?,?,?,?,?)",
                   (user_message_id, conversation_id, seq + 1, "user", client_message_id, None,
                    dumps(user_body), "received", now))
        parts = list(output.message_parts or [answer])
        next_seq = seq + 2
        for part in parts:
            db.execute("INSERT INTO message_index VALUES (?,?,?,?,?,?,?,?,?)",
                       (part.message_id, conversation_id, next_seq, "assistant", None, user_message_id,
                        part.model_dump_json(), "delivered", now))
            db.execute("INSERT INTO answer_runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                       (_new_id(), part.message_id, profile.version, output.knowledge_read_id,
                        output.model_version, output.prompt_version, output.gate_version, part.status,
                        part.trace_id, dumps(part.stage_ms or output.stage_ms), now))
            citations = {c.citation_id: c for c in part.citations}
            for claim in part.claims:
                for cid in claim.citation_ids:
                    c = citations.get(cid)
                    if c is None:
                        continue
                    db.execute("INSERT INTO answer_citations VALUES (?,?,?,?,?,?,?,?)",
                               (part.message_id, claim.claim_id, cid, c.record_id, c.revision_id,
                                c.evidence_id, c.source_url, c.verified_at.isoformat()))
            next_seq += 1
        for p in parts[-1].profile_change_proposals:
            db.execute("INSERT INTO profile_change_proposals VALUES (?,?,?,?,?,?,?, 'open', ?, NULL)",
                       (p.proposal_id, p.profile_id, p.field_code, dumps(p.old_value) if p.old_value is not None
                        else None, dumps(p.new_value), user_message_id, p.basis, now))
        for p in parts[-1].bank_relationship_proposals:
            db.execute("""INSERT INTO bank_relationship_proposals
                (proposal_id,profile_id,market,product_id,product_name_snapshot,holder_relation,
                 proposed_status,existing_relationship_id,source_message_id,basis,status,created_at,decided_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,'open',?,NULL)""",
                       (p.proposal_id, p.profile_id, p.market, p.product_id, p.product_name,
                        p.holder_relation, p.proposed_status, p.existing_relationship_id,
                        user_message_id, p.basis, now))
        advice_items = list(output.advice_items or [])
        if not advice_items and output.advice:
            advice_items = [output.advice]
        for item in advice_items:
            topic = item["topic"]
            current = db.execute("SELECT version FROM advice_current WHERE profile_id=? AND topic=?",
                                 (profile.profile_id, topic)).fetchone()
            version = (current["version"] if current else 0) + 1
            payload = dumps(item["payload"])
            db.execute("""INSERT INTO advice_current VALUES (?,?,?,?,?, 'current', ?)
                ON CONFLICT(profile_id, topic) DO UPDATE SET version=excluded.version,
                source_message_id=excluded.source_message_id, payload_json=excluded.payload_json,
                status='current', updated_at=excluded.updated_at""",
                       (profile.profile_id, topic, version, answer.message_id, payload, now))
            db.execute("INSERT INTO advice_history VALUES (?,?,?,?,?,?)",
                       (profile.profile_id, topic, version, payload, answer.message_id, now))
            domain_payload = item.get("payload", {}).get("domain_payload") or {}
            if topic == "account" and domain_payload.get("account_region"):
                scope_key = domain_payload["account_region"]
                scoped = db.execute("""SELECT version FROM advice_scoped_current
                    WHERE profile_id=? AND topic='account' AND scope_key=?""",
                                    (profile.profile_id, scope_key)).fetchone()
                scoped_version = (scoped["version"] if scoped else 0) + 1
                db.execute("""INSERT INTO advice_scoped_current
                    (profile_id,topic,scope_key,version,source_message_id,payload_json,status,updated_at)
                    VALUES (?,'account',?,?,?,?, 'current', ?)
                    ON CONFLICT(profile_id,topic,scope_key) DO UPDATE SET
                      version=excluded.version,source_message_id=excluded.source_message_id,
                      payload_json=excluded.payload_json,status='current',updated_at=excluded.updated_at""",
                           (profile.profile_id, scope_key, scoped_version,
                            answer.message_id, payload, now))
            conversation_state = domain_payload.get("conversation_state")
            if topic == "account" and isinstance(conversation_state, dict):
                state_version = int(conversation_state.get("version") or 0)
                db.execute("""INSERT INTO domain_conversation_state
                    (session_id,domain,version,state_json,updated_at) VALUES (?, 'bank_account', ?, ?, ?)
                    ON CONFLICT(session_id, domain) DO UPDATE SET version=excluded.version,
                    state_json=excluded.state_json, updated_at=excluded.updated_at""",
                           (conversation_id, state_version, dumps(conversation_state), now))

    # ---------- reactions / M11 / snapshots ----------

    def react(self, user_id, message_id, kind):
        row = self.store.one("""SELECT m.message_id FROM message_index m JOIN sessions s USING(session_id)
            WHERE m.message_id=? AND s.user_id=? AND m.role='assistant'""", (message_id, user_id))
        if not row:
            raise not_found("消息")
        if kind not in ("like", "dislike"):
            raise bad_request("kind 只能是 like / dislike")
        with self.store.transaction() as db:
            db.execute("""INSERT INTO message_reactions VALUES (?,?,?,?)
                ON CONFLICT(message_id, user_id) DO UPDATE SET kind=excluded.kind""",
                       (message_id, user_id, kind, self._ts()))
        return {"message_id": message_id, "kind": kind, "profile_changed": False}

    def get_advice(self, user_id, profile_id, topic):
        self._owned_profile_row(user_id, profile_id)
        row = self.store.one("SELECT * FROM advice_current WHERE profile_id=? AND topic=?", (profile_id, topic))
        if not row:
            raise not_found("建议")
        row["payload"] = json.loads(row.pop("payload_json"))
        self._enrich_legacy_card_advice(row)
        return row

    def list_advice(self, user_id, profile_id):
        self._owned_profile_row(user_id, profile_id)
        rows = self.store.all("SELECT * FROM advice_current WHERE profile_id=?", (profile_id,))
        for row in rows:
            row["payload"] = json.loads(row.pop("payload_json"))
            self._enrich_legacy_card_advice(row)
        scoped_rows = self.store.all("""SELECT * FROM advice_scoped_current
            WHERE profile_id=? ORDER BY topic, scope_key""", (profile_id,))
        for row in scoped_rows:
            row["payload"] = json.loads(row.pop("payload_json"))
        scoped = {}
        for row in scoped_rows:
            scoped.setdefault(row["topic"], {})[row["scope_key"]] = row
        return {"profile_id": profile_id, "advice": {r["topic"]: r for r in rows},
                "scoped_advice": scoped}

    def _enrich_legacy_card_advice(self, row):
        """Render older saved card advice without rewriting the user's database."""
        if row["topic"] != "credit_card" or "cards" in row["payload"]:
            return
        source = self.store.one("SELECT body_json FROM message_index WHERE message_id=?",
                                (row["source_message_id"],))
        if not source:
            return
        body = json.loads(source["body_json"])
        steps = body.get("answer", {}).get("steps", [])
        row["payload"]["cards"] = card_summaries_from_steps(steps)
        row["payload"]["result_status"] = body.get("status", "answered")
        row["payload"]["preview"] = any(step.startswith("提前了解：") for step in steps)

    # ---------- feedback ----------

    def submit_feedback(self, user_id, message_id, kind, note=""):
        """wrong → dislike 并开纠错工单（返回编号）。"""
        if kind != "wrong":
            raise bad_request("kind 只能是 wrong")
        self.react(user_id, message_id, "dislike")
        note = (note or "").strip()[:2000] or "（用户未填写说明）"
        now = self.clock()
        feedback_id = f"FB-{now:%Y%m%d}-{secrets.token_hex(3).upper()}"
        with self.store.transaction() as db:
            db.execute("INSERT INTO feedback_tickets VALUES (?,?,?,?, 'open', ?)",
                       (feedback_id, message_id, user_id, note, now.isoformat()))
        return {"message_id": message_id, "kind": kind, "feedback_id": feedback_id, "status": "open"}

    def list_feedback(self, user_id):
        rows = self.store.all("""SELECT f.feedback_id, f.message_id, f.note, f.status, f.created_at, m.body_json
            FROM feedback_tickets f JOIN message_index m USING(message_id)
            WHERE f.user_id=? ORDER BY f.created_at DESC""", (user_id,))
        for row in rows:
            body = json.loads(row.pop("body_json"))
            row["answer_status"] = body.get("status")
            row["answer_summary"] = (body.get("answer") or {}).get("summary", "")
            row["profile_id"] = body.get("profile_id")
        return {"tickets": rows}

    # ---------- Plan (用户可编辑待办，独立于 Advice) ----------

    def list_plan(self, user_id, profile_id):
        self._owned_profile_row(user_id, profile_id)
        rows = self.store.all("SELECT item_id, text, done, created_at, updated_at FROM plan_items "
                              "WHERE profile_id=? ORDER BY created_at, rowid", (profile_id,))
        for row in rows:
            row["done"] = bool(row["done"])
        return {"profile_id": profile_id, "items": rows}

    def add_plan_item(self, user_id, profile_id, text):
        self._owned_profile_row(user_id, profile_id)
        if not isinstance(text, str) or not text.strip():
            raise bad_request("待办内容不能为空")
        item_id, now = _new_id(), self._ts()
        with self.store.transaction() as db:
            db.execute("INSERT INTO plan_items VALUES (?,?,?,0,?,?)", (item_id, profile_id, text.strip()[:500],
                                                                       now, now))
        return {"item_id": item_id, "text": text.strip()[:500], "done": False}

    def _owned_plan_item(self, user_id, item_id):
        row = self.store.one("""SELECT i.* FROM plan_items i JOIN profiles p USING(profile_id)
            WHERE i.item_id=? AND p.user_id=?""", (item_id, user_id))
        if not row:
            raise not_found("待办")
        return row

    def update_plan_item(self, user_id, item_id, *, text=None, done=None):
        row = self._owned_plan_item(user_id, item_id)
        if text is not None and (not isinstance(text, str) or not text.strip()):
            raise bad_request("待办内容不能为空")
        if done is not None and not isinstance(done, bool):
            raise bad_request("done 必须是布尔值")
        new_text = text.strip()[:500] if text is not None else row["text"]
        new_done = bool(row["done"]) if done is None else done
        with self.store.transaction() as db:
            db.execute("UPDATE plan_items SET text=?, done=?, updated_at=? WHERE item_id=?",
                       (new_text, int(new_done), self._ts(), item_id))
        return {"item_id": item_id, "text": new_text, "done": new_done}

    def delete_plan_item(self, user_id, item_id):
        self._owned_plan_item(user_id, item_id)
        with self.store.transaction() as db:
            db.execute("DELETE FROM plan_items WHERE item_id=?", (item_id,))
        return {"item_id": item_id, "deleted": True}

    def knowledge_snapshot(self, revision_id):
        snapshot = getattr(self.knowledge, "snapshot", lambda _: None)(revision_id)
        if not snapshot:
            raise not_found("资料快照")
        return snapshot
