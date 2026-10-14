"""S1 业务服务：M01 身份授权、M02/M03 档案与提案、M04 会话、M11 Advice。

所有读写都按 user_id 做对象级授权；他人的资源与不存在的资源返回同一个 NOT_FOUND，不泄露内容。
"""
import json
import secrets
import sqlite3
import uuid
from datetime import datetime, timezone

from . import clarify
from .gate import GATE_VERSION
from .generate import PROMPT_VERSION
from .models import AnswerResult, Profile, ProfileChangeProposal, ProfileField
from .runner import Agent2Runner
from .store import dumps

DEFAULT_PROFILE_FIELDS = ("role", "destination_country", "visa_category", "school", "planned_start_date")


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
    def __init__(self, store, knowledge, *, clock=utc_now, generator=None, demo_enabled=True, suggest=None):
        self.store = store
        self.knowledge = knowledge
        self.clock = clock
        self.demo_enabled = demo_enabled
        self.runner = Agent2Runner(knowledge, clock, generator=generator, suggest=suggest)

    def _ts(self):
        return self.clock().isoformat()

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
            token = secrets.token_urlsafe(24)
            db.execute("INSERT INTO sessions VALUES (?,?,?)", (token, user_id, self._ts()))
        return {"token": token, "user_id": user_id, "synthetic": True,
                "notice": "模拟身份，仅用于 Demo，不是真实银行账户登录。"}

    def user_for_token(self, token):
        row = self.store.one("SELECT user_id FROM sessions WHERE token=?", (token or "",))
        if not row:
            raise ServiceError("UNAUTHENTICATED", "需要登录", 401)
        return row["user_id"]

    def me(self, user_id):
        user = self.store.one("SELECT user_id, display_name, synthetic FROM users WHERE user_id=?", (user_id,))
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
        fields = {r["field_code"]: ProfileField(value=json.loads(r["value_json"]) if r["value_json"] else None,
                                                status=r["status"], source=r["source"],
                                                confirmed_at=r["confirmed_at"])
                  for r in self.store.all("SELECT * FROM profile_fields WHERE profile_id=?", (profile_id,))}
        return Profile(profile_id=profile_id, user_id=user_id, version=row["version"],
                       synthetic=bool(row["synthetic"]), fields=fields)

    @staticmethod
    def _check_values(values):
        if not isinstance(values, dict):
            raise bad_request("fields 必须是对象")
        for code, value in values.items():
            error = clarify.validate(code, value)
            if error:
                raise bad_request(f"{clarify.label(code)}：{error}")

    def _write_fields(self, db, profile_id, values, source):
        now = self._ts()
        for code, value in values.items():
            db.execute("""INSERT INTO profile_fields VALUES (?,?,?, 'confirmed', ?, ?)
                ON CONFLICT(profile_id, field_code) DO UPDATE SET value_json=excluded.value_json,
                status='confirmed', source=excluded.source, confirmed_at=excluded.confirmed_at""",
                       (profile_id, code, dumps(value), source, now))

    def create_profile(self, user_id, fields=None, *, synthetic=False):
        values = fields or {}
        self._check_values(values)
        profile_id = _new_id()
        now = self._ts()
        with self.store.transaction() as db:
            db.execute("INSERT INTO profiles VALUES (?,?,1,?,?,?)", (profile_id, user_id, int(synthetic), now, now))
            for code in DEFAULT_PROFILE_FIELDS:
                db.execute("INSERT INTO profile_fields VALUES (?,?,NULL,'unknown',NULL,NULL)", (profile_id, code))
            self._write_fields(db, profile_id, values, "demo" if synthetic else "user")
        return self.get_profile(user_id, profile_id)

    def patch_profile(self, user_id, profile_id, *, expected_version, set_fields=None, clear_fields=()):
        set_fields = set_fields or {}
        self._check_values(set_fields)
        unknown = [c for c in clear_fields if c not in clarify.FIELDS]
        if unknown:
            raise bad_request(f"未知字段：{', '.join(unknown)}")
        with self.store.transaction() as db:
            row = db.execute("SELECT version FROM profiles WHERE profile_id=? AND user_id=?",
                             (profile_id, user_id)).fetchone()
            if not row:
                raise not_found("档案")
            self._bump(db, profile_id, row["version"], expected_version)
            self._write_fields(db, profile_id, set_fields, "user")
            for code in clear_fields:
                db.execute("""INSERT INTO profile_fields VALUES (?,?,NULL,'unknown',NULL,NULL)
                    ON CONFLICT(profile_id, field_code) DO UPDATE SET value_json=NULL, status='unknown',
                    source=NULL, confirmed_at=NULL""", (profile_id, code))
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
            self._write_fields(db, row["profile_id"], {row["field_code"]: json.loads(row["new_value"])}, "user")
            db.execute("UPDATE profile_change_proposals SET status='confirmed', decided_at=? WHERE proposal_id=?",
                       (self._ts(), proposal_id))
        return self.get_profile(user_id, row["profile_id"])

    def dismiss_proposal(self, user_id, proposal_id):
        self._owned_proposal(user_id, proposal_id)
        with self.store.transaction() as db:
            db.execute("UPDATE profile_change_proposals SET status='dismissed', decided_at=? "
                       "WHERE proposal_id=? AND status='open'", (self._ts(), proposal_id))
        return {"proposal_id": proposal_id, "status": "dismissed"}

    # ---------- M04 ----------

    def create_conversation(self, user_id, profile_id):
        profile = self._owned_profile_row(user_id, profile_id)
        conversation_id = _new_id()
        with self.store.transaction() as db:
            db.execute("INSERT INTO conversations VALUES (?,?,?,?,?)",
                       (conversation_id, user_id, profile_id, profile["version"], self._ts()))
        return {"conversation_id": conversation_id, "profile_id": profile_id,
                "profile_version": profile["version"]}

    def list_conversations(self, user_id):
        return self.store.all("SELECT conversation_id, profile_id, profile_version, created_at "
                              "FROM conversations WHERE user_id=? ORDER BY created_at", (user_id,))

    def _owned_conversation(self, user_id, conversation_id):
        row = self.store.one("SELECT * FROM conversations WHERE conversation_id=? AND user_id=?",
                             (conversation_id, user_id))
        if not row:
            raise not_found("会话")
        return row

    def list_messages(self, user_id, conversation_id, *, after_seq=0, limit=50):
        self._owned_conversation(user_id, conversation_id)
        rows = self.store.all("""SELECT message_id, seq, role, client_message_id, reply_to, body_json, status,
            created_at FROM message_index WHERE conversation_id=? AND seq>? ORDER BY seq LIMIT ?""",
                              (conversation_id, after_seq, min(max(int(limit), 1), 200)))
        for row in rows:
            row["body"] = json.loads(row.pop("body_json"))
        return {"messages": rows, "next_after_seq": rows[-1]["seq"] if rows else after_seq}

    def _conversation_context(self, conversation_id):
        facts, topic, asked, last_version = {}, None, (), None
        for row in self.store.all("SELECT role, body_json FROM message_index WHERE conversation_id=? ORDER BY seq",
                                  (conversation_id,)):
            body = json.loads(row["body_json"])
            if row["role"] == "user":
                facts.update(body.get("facts") or {})
                topic = body.get("topic") or topic
            else:
                asked = tuple(q["field"] for q in body.get("follow_up_questions", []))
                last_version = body.get("profile_version", last_version)
        return facts, topic, asked, last_version

    def _stored_reply(self, conversation_id, client_message_id):
        row = self.store.one("""SELECT a.body_json FROM message_index u JOIN message_index a
            ON a.reply_to=u.message_id WHERE u.conversation_id=? AND u.role='user' AND u.client_message_id=?""",
                             (conversation_id, client_message_id))
        return AnswerResult.model_validate_json(row["body_json"]) if row else None

    def post_message(self, user_id, conversation_id, text, client_message_id):
        conversation = self._owned_conversation(user_id, conversation_id)
        if not isinstance(client_message_id, str) or not client_message_id.strip():
            raise bad_request("需要 client_message_id（幂等键）")
        stored = self._stored_reply(conversation_id, client_message_id)
        if stored:
            return stored
        profile = self.get_profile(user_id, conversation["profile_id"])
        facts, topic, asked, last_version = self._conversation_context(conversation_id)
        user_message_id, answer_id = _new_id(), _new_id()
        output = self.runner.run(profile=profile, text=text, conversation_facts=facts, conversation_topic=topic,
                                 asked_fields=asked,
                                 expected_profile_version=last_version or conversation["profile_version"],
                                 message_id=answer_id)
        answer = output.result
        answer.profile_change_proposals = self._proposals_for(profile, output, user_message_id)
        try:
            with self.store.transaction() as db:
                self._persist(db, conversation_id, profile, text, client_message_id, user_message_id, output)
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
        for code, value in output.turn_facts.items():
            if profile.confirmed(code) == value or (code, dumps(value)) in already:
                continue
            proposals.append(ProfileChangeProposal(
                proposal_id=_new_id(), profile_id=profile.profile_id, field_code=code,
                old_value=profile.confirmed(code), new_value=value, source_message_id=user_message_id,
                basis=f"用户消息：「{output.turn_basis.get(code, '')}」"))
        return proposals

    def _persist(self, db, conversation_id, profile, text, client_message_id, user_message_id, output):
        answer = output.result
        now = self._ts()
        seq = db.execute("SELECT COALESCE(MAX(seq),0) FROM message_index WHERE conversation_id=?",
                         (conversation_id,)).fetchone()[0]
        user_body = {"text": text, "facts": output.turn_facts, "topic": output.topic}
        db.execute("INSERT INTO message_index VALUES (?,?,?,?,?,?,?,?,?)",
                   (user_message_id, conversation_id, seq + 1, "user", client_message_id, None,
                    dumps(user_body), "received", now))
        body = answer.model_dump_json()
        db.execute("INSERT INTO message_index VALUES (?,?,?,?,?,?,?,?,?)",
                   (answer.message_id, conversation_id, seq + 2, "assistant", None, user_message_id,
                    body, "delivered", now))
        db.execute("INSERT INTO answer_runs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (_new_id(), answer.message_id, profile.version, output.knowledge_read_id, output.model_version,
                    PROMPT_VERSION, GATE_VERSION, answer.status, answer.trace_id, dumps(output.stage_ms), now))
        citations = {c.citation_id: c for c in answer.citations}
        for claim in answer.claims:
            for cid in claim.citation_ids:
                c = citations[cid]
                db.execute("INSERT INTO answer_citations VALUES (?,?,?,?,?,?,?,?)",
                           (answer.message_id, claim.claim_id, cid, c.record_id, c.revision_id, c.evidence_id,
                            c.source_url, c.verified_at.isoformat()))
        for p in answer.profile_change_proposals:
            db.execute("INSERT INTO profile_change_proposals VALUES (?,?,?,?,?,?,?, 'open', ?, NULL)",
                       (p.proposal_id, p.profile_id, p.field_code, dumps(p.old_value) if p.old_value is not None
                        else None, dumps(p.new_value), user_message_id, p.basis, now))
        if output.advice:
            topic = output.advice["topic"]
            current = db.execute("SELECT version FROM advice_current WHERE profile_id=? AND topic=?",
                                 (profile.profile_id, topic)).fetchone()
            version = (current["version"] if current else 0) + 1
            payload = dumps(output.advice["payload"])
            db.execute("""INSERT INTO advice_current VALUES (?,?,?,?,?, 'current', ?)
                ON CONFLICT(profile_id, topic) DO UPDATE SET version=excluded.version,
                source_message_id=excluded.source_message_id, payload_json=excluded.payload_json,
                status='current', updated_at=excluded.updated_at""",
                       (profile.profile_id, topic, version, answer.message_id, payload, now))
            db.execute("INSERT INTO advice_history VALUES (?,?,?,?,?,?)",
                       (profile.profile_id, topic, version, payload, answer.message_id, now))

    # ---------- reactions / M11 / snapshots ----------

    def react(self, user_id, message_id, kind):
        row = self.store.one("""SELECT m.message_id FROM message_index m JOIN conversations c USING(conversation_id)
            WHERE m.message_id=? AND c.user_id=? AND m.role='assistant'""", (message_id, user_id))
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
        return row

    def knowledge_snapshot(self, revision_id):
        snapshot = getattr(self.knowledge, "snapshot", lambda _: None)(revision_id)
        if not snapshot:
            raise not_found("资料快照")
        return snapshot
