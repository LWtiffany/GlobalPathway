"""S1 业务服务：M01 身份授权、M02/M03 档案与提案、M04 会话、M11 Advice。

所有读写都按 user_id 做对象级授权；他人的资源与不存在的资源返回同一个 NOT_FOUND，不泄露内容。
"""
import asyncio
import json
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from . import clarify
from .models import AnswerResult, Profile, ProfileChangeProposal, ProfileField
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
    def __init__(self, store, knowledge, *, clock=utc_now, generator=None, demo_enabled=True, suggest=None,
                 credit_card_repository=None, llm_components=None):
        self.store = store
        self.knowledge = knowledge
        self.clock = clock
        self.demo_enabled = demo_enabled
        self.runner = Agent2Runner(knowledge, clock, generator=generator, suggest=suggest)
        llm = llm_components if llm_components is not None else build_llm_components()
        self.v2_workflow = build_default_workflow(
            credit_card_repository=credit_card_repository,
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
                     ask_priority,ask_only_when_needed,question_template,schema_version,active,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,1,1,?)
                    ON CONFLICT(field_code) DO UPDATE SET display_name=excluded.display_name,
                    value_type=excluded.value_type, options_json=excluded.options_json,
                    domains_json=excluded.domains_json, sensitivity=excluded.sensitivity,
                    freshness_days=excluded.freshness_days, ask_priority=excluded.ask_priority,
                    ask_only_when_needed=excluded.ask_only_when_needed,
                    question_template=excluded.question_template, schema_version=excluded.schema_version,
                    active=excluded.active, updated_at=excluded.updated_at""",
                           (code, item["label"], item["kind"], dumps(item["options"]),
                            dumps(item["domains"]), item["sensitivity"], item["freshness_days"],
                            item["ask_priority"], int(item["ask_only_when_needed"]), item["question"], now))

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
            db.execute("INSERT INTO auth_sessions VALUES (?,?,?)", (token, user_id, self._ts()))
        return {"token": token, "user_id": user_id, "synthetic": True,
                "notice": "模拟身份，仅用于 Demo，不是真实银行账户登录。"}

    def user_for_token(self, token):
        row = self.store.one("SELECT user_id FROM auth_sessions WHERE token=?", (token or "",))
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
        self._check_values(values)
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
        self._check_values(set_fields)
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

    def dismiss_proposal(self, user_id, proposal_id):
        self._owned_proposal(user_id, proposal_id)
        with self.store.transaction() as db:
            db.execute("UPDATE profile_change_proposals SET status='dismissed', decided_at=? "
                       "WHERE proposal_id=? AND status='open'", (self._ts(), proposal_id))
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

    def _conversation_context(self, conversation_id):
        facts, topic, asked, last_version = {}, None, (), None
        active_intents: list[str] = []
        for row in self.store.all("SELECT role, body_json FROM message_index WHERE session_id=? ORDER BY seq",
                                  (conversation_id,)):
            body = json.loads(row["body_json"])
            if row["role"] == "user":
                facts.update(body.get("facts") or {})
                topic = body.get("topic") or topic
                if body.get("active_intents"):
                    active_intents = list(body["active_intents"])
            else:
                asked = tuple(q["field"] for q in body.get("follow_up_questions", []))
                last_version = body.get("profile_version", last_version)
        return facts, topic, asked, last_version, active_intents

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

    def _apply_understanding(self, request: OrchestrationInput):
        """Merge LLM intents/facts into the routing request (rules ∪ model). Fail soft."""
        port = self.v2_workflow.dependencies.understanding
        if (port is None or request.input_mode != "free_text" or not request.text.strip()
                or request.understanding_applied):
            return request, False
        try:
            catalog = clarify.field_catalog()
            raw = asyncio.run(port.understand(request, catalog))
            facts, basis, intents = validate_understanding(
                raw, request, set(catalog), clarify.validate)
            enriched = request.model_copy(update={
                "conversation_intents": list(dict.fromkeys([
                    *request.conversation_intents, *intents])),
                "llm_intents": list(dict.fromkeys(intents)),
                "understood_facts": facts,
                "understood_basis": basis,
                "understanding_applied": True,
            })
            return enriched, True
        except Exception:
            return request, False

    def post_message(self, user_id, conversation_id, text, client_message_id, *, confirmed_fields=None,
                     expected_profile_version=None, cleared_fields=None):
        conversation = self._owned_conversation(user_id, conversation_id)
        if not isinstance(client_message_id, str) or not client_message_id.strip():
            raise bad_request("需要 client_message_id（幂等键）")
        stored = self._stored_reply(conversation_id, client_message_id)
        if stored:
            return stored
        facts, topic, asked, last_version, active_intents = self._conversation_context(conversation_id)
        user_message_id, answer_id = _new_id(), _new_id()
        confirmed_fields = confirmed_fields or {}
        cleared_fields = list(dict.fromkeys(cleared_fields or []))
        if confirmed_fields:
            if not isinstance(confirmed_fields, dict) or not set(confirmed_fields) <= set(asked):
                raise bad_request("只能保存当前追问要求的画像字段")
            self._check_values(confirmed_fields)
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
        # Structured follow-ups continue the open multi-domain turn, but a chip
        # answer is scoped to the domain(s) that own the confirmed field(s) so
        # confirming want_credit_card does not re-emit bank-account advice.
        if asked and topic:
            resolved_topic = topic
            planned_intents = list(active_intents) if active_intents else intents_for_session_topic(
                topic, facts)
            if confirmed_fields:
                planned_intents = intents_for_confirmed_fields(
                    confirmed_fields, planned_intents)
        else:
            route_request = OrchestrationInput(
                text=text,
                profile_id=profile.profile_id,
                profile_version=profile.version,
                asked_fields=list(asked),
                conversation_intents=(
                    list(active_intents) if active_intents
                    else intents_for_session_topic(topic, facts) if topic else []),
            )
            route_request, understanding_applied = self._apply_understanding(route_request)
            understood_facts = dict(route_request.understood_facts)
            understood_basis = dict(route_request.understood_basis)
            llm_intents = list(route_request.llm_intents)
            plan = asyncio.run(RuleTaskPlanner().plan(route_request))
            planned_intents = [task.intent for task in plan.tasks]
            session_topic = session_topic_for_intents(planned_intents)
            if session_topic:
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
            output = run_v2(self.v2_workflow, profile=profile, text=text,
                            conversation_facts=facts, asked_fields=asked,
                            message_id=answer_id,
                            structured_selection=bool(asked),
                            conversation_intents=v2_intents,
                            understood_facts=understood_facts,
                            understood_basis=understood_basis,
                            understanding_applied=understanding_applied,
                            llm_intents=llm_intents)
        else:
            output = self.runner.run(profile=profile, text=text, conversation_facts=facts, conversation_topic=topic,
                                     asked_fields=asked,
                                     expected_profile_version=last_version or conversation["profile_version"],
                                     message_id=answer_id)
        answer = output.result
        if confirmed_fields:
            equivalent = {"applicant_age_band": "applicant_age",
                          "annual_income_band_hkd": "annual_income_hkd"}
            for code in confirmed_fields:
                output.turn_facts.pop(code, None)
                output.turn_basis.pop(code, None)
                alternate = equivalent.get(code)
                if alternate:
                    output.turn_facts.pop(alternate, None)
                    output.turn_basis.pop(alternate, None)
        proposals = self._proposals_for(profile, output, user_message_id)
        answer.profile_change_proposals = proposals
        parts = list(output.message_parts or [answer])
        if parts:
            parts[-1].profile_change_proposals = proposals
            output.message_parts = parts
            output.result = parts[-1]
            answer = parts[-1]
        try:
            with self.store.transaction() as db:
                self._persist(db, conversation_id, profile, text, client_message_id, user_message_id, output,
                              confirmed_fields=confirmed_fields, cleared_fields=cleared_fields)
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

    def _persist(self, db, conversation_id, profile, text, client_message_id, user_message_id, output,
                 *, confirmed_fields=None, cleared_fields=None):
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
        return {"profile_id": profile_id, "advice": {r["topic"]: r for r in rows}}

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
        """helpful → like；wrong → dislike 并开纠错工单（返回编号）。"""
        if kind not in ("helpful", "wrong"):
            raise bad_request("kind 只能是 helpful / wrong")
        self.react(user_id, message_id, "like" if kind == "helpful" else "dislike")
        if kind == "helpful":
            return {"message_id": message_id, "kind": kind, "feedback_id": None}
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
