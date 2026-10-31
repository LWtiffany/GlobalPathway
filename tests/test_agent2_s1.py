"""S1 固定验收案例 S1-01 … S1-10（docs/agent2-s1-uk-student-visa.md §7）。"""
import json
import unittest
from datetime import datetime, timedelta, timezone

from agent2 import api
from agent2.generate import TemplateGenerator
from agent2.knowledge import DEFAULT_FIXTURE, FixtureKnowledgeRead
from agent2.service import Agent2Service, ServiceError
from agent2.store import Store

NOW = datetime(2026, 10, 14, tzinfo=timezone.utc)
UK_STUDENT = {"destination_country": "UK", "visa_category": "student_visa"}
# Condition fields resolved so the runner returns the materials list (ask-first UX).
UK_STUDENT_READY = {
    **UK_STUDENT,
    "applicant_age": 20,
    "funding_source": "own_or_parents",
    "tb_listed_country_resident": False,
}


def fixture_records():
    return json.loads(DEFAULT_FIXTURE.read_text(encoding="utf-8"))["records"]


class S1Base(unittest.TestCase):
    def make(self, records=None, clock=NOW, generator=None):
        knowledge = FixtureKnowledgeRead(records or fixture_records(), "test")
        self.knowledge = knowledge
        store = Store()
        self.addCleanup(store.close)
        self.service = Agent2Service(store, knowledge, clock=lambda: clock, generator=generator)
        self.user = self.service.demo_login("alice")["user_id"]
        return self.service

    def ask(self, text, fields=None, client_id="m1"):
        profile = self.service.create_profile(self.user, fields or {}, synthetic=True)
        conversation = self.service.create_conversation(self.user, profile.profile_id)
        answer = self.service.post_message(self.user, conversation["conversation_id"], text, client_id)
        return profile, conversation, answer


class S1Cases(S1Base):
    def test_overseas_spend_routes_to_fx_checklist_without_region_scope_claim(self):
        self.make()
        _, _, answer = self.ask("我想了解在香港境外消费怎么刷卡")

        # P2: overseas spend is a minimal fx closed loop (destination already in text).
        self.assertEqual("answered", answer.status)
        self.assertIn("境外消费", answer.answer.summary)
        self.assertIn("中国香港", answer.answer.summary)
        self.assertNotIn("只覆盖", answer.answer.summary)
        self.assertTrue(any("实时汇率" in note or "报价" in note for note in answer.answer.limitations))

    def test_s1_01_unknown_category_asks_only_for_it_then_answers_and_history_restores(self):
        self.make()
        profile, conversation, answer = self.ask("去英国办签证要什么材料")
        self.assertEqual(answer.status, "needs_input")
        self.assertEqual([q.field for q in answer.follow_up_questions], ["visa_category"])
        self.assertEqual(answer.missing_fields, ["visa_category"])
        self.assertEqual(answer.answer.materials, [])
        question = answer.follow_up_questions[0]
        self.assertEqual((question.question_code, question.profile_version), ("ask_visa_category", 1))

        second = self.service.post_message(self.user, conversation["conversation_id"], "Student visa", "m2")
        # After the category is known, condition fields are still asked before the list.
        self.assertEqual(second.status, "needs_input")
        self.assertTrue(second.follow_up_questions)
        self.assertEqual(second.answer.materials, [])
        history = self.service.list_messages(self.user, conversation["conversation_id"])["messages"]
        self.assertEqual([m["role"] for m in history], ["user", "assistant", "user", "assistant"])
        self.assertEqual(history[3]["body"]["message_id"], second.message_id)
        self.assertEqual(self.service.get_profile(self.user, profile.profile_id).version, 1)

    def test_s1_02_answered_with_expandable_synthetic_citations(self):
        self.make()
        _, _, answer = self.ask("需要准备哪些签证材料", UK_STUDENT_READY)
        self.assertEqual(answer.status, "answered")
        self.assertTrue(answer.synthetic)
        required = {m.document_code for m in answer.answer.materials if m.applicability == "required"}
        self.assertEqual(required, {"passport", "cas", "proof_of_funds"})
        citations = {c.citation_id: c for c in answer.citations}
        for material in answer.answer.materials:
            citation = citations[material.citation_id]
            self.assertTrue(citation.synthetic)
            self.assertIsNone(citation.source_url)  # example.invalid 不展示为真实来源
            snapshot = self.service.knowledge_snapshot(citation.revision_id)
            self.assertIn(citation.quote, snapshot["snapshot"])
            self.assertEqual(citation.verified_at, datetime(2026, 10, 13, tzinfo=timezone.utc))
        for claim in answer.claims:
            self.assertTrue(set(claim.citation_ids) <= set(citations))
        self.assertTrue(answer.answer.limitations[0].startswith("【模拟资料】"))

    def test_s1_03_unknown_or_unmet_condition_is_not_unconditional(self):
        self.make()
        _, _, answer = self.ask("需要准备哪些签证材料", UK_STUDENT)
        self.assertEqual(answer.status, "needs_input")
        self.assertTrue(answer.follow_up_questions)
        self.assertIn(answer.follow_up_questions[0].field,
                      {"applicant_age", "funding_source", "tb_listed_country_resident"})
        self.assertEqual(answer.answer.materials, [])

        _, _, adult = self.ask("需要准备哪些签证材料", UK_STUDENT_READY, client_id="m9")
        self.assertEqual(adult.status, "answered")
        by_code = {m.document_code: m for m in adult.answer.materials}
        self.assertEqual(by_code["atas"].applicability, "unverified")
        self.assertNotIn("parental_consent", by_code)

    def test_s1_04_pending_or_withdrawn_is_never_a_definite_fact(self):
        for status in ("PENDING_REVIEW", "WITHDRAWN"):
            with self.subTest(status=status):
                records = fixture_records()
                records[0]["status"] = status
                self.make(records, clock=NOW)
                _, _, answer = self.ask("需要准备哪些签证材料", UK_STUDENT)
                self.assertEqual(answer.status, "needs_verification")
                self.assertEqual((answer.answer.materials, answer.citations), ([], []))
                self.assertTrue(any("gov.uk" in a for a in answer.next_actions))

    def test_s1_04b_active_past_former_sla_still_answers_from_db(self):
        # Freshness SLA is not an answer gate; ACTIVE published rows remain usable.
        records = fixture_records()
        self.make(records, clock=NOW + timedelta(days=30))
        _, _, answer = self.ask("需要准备哪些签证材料", UK_STUDENT_READY)
        self.assertEqual(answer.status, "answered")
        self.assertTrue(answer.answer.materials)

    def test_s1_05_conflicting_requirements_are_all_shown_not_picked(self):
        records = fixture_records()
        other = json.loads(json.dumps(records[0]))
        other.update(record_id="fixture-uk-sv-documents-b", revision_id="fixture-uk-sv-documents-b-r1")
        other["source"]["name"] = "合成来源示例 B（模拟资料）"
        cas = next(r for r in other["requirements"] if r["document_code"] == "cas")
        cas.update(applicability_status="conditional",
                   condition=[{"field": "applicant_age", "op": "gte", "value": 18, "text": "申请人年满 18 岁"}])
        other["requirements"] = [cas]
        self.make(records + [other])
        _, _, answer = self.ask("需要准备哪些签证材料", UK_STUDENT_READY)
        self.assertEqual(answer.status, "needs_verification")
        self.assertEqual([c.document_code for c in answer.conflicts], ["cas"])
        variants = answer.conflicts[0].variants
        self.assertEqual(len(variants), 2)
        citation_ids = {c.citation_id for c in answer.citations}
        self.assertTrue({v["citation_id"] for v in variants} <= citation_ids)
        self.assertNotIn("cas", {m.document_code for m in answer.answer.materials})

    def test_s1_06_invented_citation_is_blocked_and_core_loss_means_cannot_answer(self):
        class InventsCitation(TemplateGenerator):
            def __init__(self, codes):
                self.codes = codes

            def draft(self, **kwargs):
                draft = super().draft(**kwargs)
                for material in draft.materials:
                    if material.document_code in self.codes:
                        material.citation_id = "s99"
                return draft

        self.make(generator=InventsCitation({"cas"}))
        _, _, answer = self.ask("需要准备哪些签证材料", UK_STUDENT_READY)
        self.assertEqual(answer.status, "answered")
        self.assertNotIn("cas", {m.document_code for m in answer.answer.materials})
        self.assertTrue(any("未通过引用" in limit for limit in answer.answer.limitations))

        self.make(generator=InventsCitation({"passport", "cas", "proof_of_funds"}))
        _, _, answer = self.ask("需要准备哪些签证材料", UK_STUDENT_READY)
        self.assertEqual(answer.status, "cannot_answer")
        self.assertEqual((answer.answer.materials, answer.claims, answer.citations), ([], [], []))

    def test_revision_withdrawn_during_generation_is_blocked(self):
        test = self

        class WithdrawsMidway(TemplateGenerator):
            def draft(self, **kwargs):
                draft = super().draft(**kwargs)
                test.knowledge.withdraw("fixture-uk-sv-documents-r1")
                return draft

        self.make(generator=WithdrawsMidway())
        _, _, answer = self.ask("需要准备哪些签证材料", UK_STUDENT_READY)
        self.assertEqual(answer.status, "cannot_answer")
        self.assertEqual(answer.citations, [])

    def test_s1_07_chinese_question_keeps_english_terms_and_original_quote(self):
        self.make()
        _, _, answer = self.ask("我去英国读书需要什么签证材料", UK_STUDENT_READY)
        cas = next(m for m in answer.answer.materials if m.document_code == "cas")
        self.assertIn("Confirmation of Acceptance for Studies (CAS)", cas.name_en)
        claim = next(c for c in answer.claims if c.requirement_key == "uk-sv-cas")
        self.assertIn("CAS", claim.text)
        quote = next(c.quote for c in answer.citations if c.citation_id == cas.citation_id)
        self.assertTrue(quote.startswith("You must provide a Confirmation of Acceptance for Studies"))

    def test_s1_08_other_users_resources_are_not_found_with_zero_body_leak(self):
        service = self.make()
        _, conversation, answer = self.ask("需要准备哪些签证材料", UK_STUDENT_READY)
        alice_token = service.demo_login("alice")["token"]
        bob_token = service.demo_login("bob")["token"]
        profile_id = conversation["profile_id"]
        cid = conversation["conversation_id"]
        status, payload, _ = api.dispatch(service, "GET", f"/v1/conversations/{cid}/messages",
                                          {"Authorization": f"Bearer {alice_token}"}, b"")
        self.assertEqual(status, 200)
        attempts = [("GET", f"/v1/conversations/{cid}/messages", b""),
                    ("POST", f"/v1/conversations/{cid}/messages", b'{"text": "hi", "client_message_id": "x"}'),
                    ("GET", f"/v1/profiles/{profile_id}", b""),
                    ("GET", f"/v1/profiles/{profile_id}/advice/visa", b""),
                    ("POST", f"/v1/messages/{answer.message_id}/reaction", b'{"kind": "like"}')]
        for method, path, body in attempts:
            with self.subTest(path=path, method=method):
                status, payload, _ = api.dispatch(
                    service, method, path, {"Authorization": f"Bearer {bob_token}"}, body)
                self.assertEqual(status, 404)
                self.assertEqual(set(payload), {"code", "message", "retryable", "trace_id"})
                leaked = json.dumps(payload, ensure_ascii=False)
                self.assertNotIn(answer.answer.summary, leaked)
                self.assertNotIn(profile_id, leaked)
        status, _, _ = api.dispatch(service, "GET", "/v1/me", {}, b"")
        self.assertEqual(status, 401)

    def test_s1_09_same_client_message_id_returns_same_result_once(self):
        self.make()
        profile, conversation, first = self.ask("需要准备哪些签证材料", UK_STUDENT_READY)
        again = self.service.post_message(self.user, conversation["conversation_id"], "需要准备哪些签证材料", "m1")
        self.assertEqual(again.message_id, first.message_id)
        self.assertEqual(len(self.service.list_messages(self.user, conversation["conversation_id"])["messages"]), 2)
        advice = self.service.get_advice(self.user, profile.profile_id, "visa")
        self.assertEqual((advice["version"], advice["source_message_id"]), (1, first.message_id))

    def test_s1_10_like_never_writes_profile_and_change_is_only_a_proposal(self):
        self.make()
        profile, conversation, answer = self.ask("需要准备哪些签证材料", UK_STUDENT_READY)
        self.service.react(self.user, answer.message_id, "like")
        self.assertEqual(self.service.get_profile(self.user, profile.profile_id), profile)

        reply = self.service.post_message(self.user, conversation["conversation_id"],
                                          "我的学校改成 帝国理工，签证材料有变化吗", "m2")
        proposal = next(p for p in reply.profile_change_proposals if p.field_code == "school")
        self.assertEqual(proposal.new_value, "帝国理工")
        unchanged = self.service.get_profile(self.user, profile.profile_id)
        self.assertEqual((unchanged.version, unchanged.confirmed("school")), (1, None))
        with self.assertRaises(ServiceError) as conflict:
            self.service.confirm_proposal(self.user, proposal.proposal_id, expected_version=0)
        self.assertEqual(conflict.exception.code, "VERSION_CONFLICT")
        updated = self.service.confirm_proposal(self.user, proposal.proposal_id, expected_version=1)
        self.assertEqual((updated.version, updated.confirmed("school")), (2, "帝国理工"))

    def test_profile_version_change_in_conversation_reassesses_quietly(self):
        self.make()
        profile, conversation, _ = self.ask("需要准备哪些签证材料", UK_STUDENT_READY)
        self.service.patch_profile(self.user, profile.profile_id, expected_version=1, set_fields={"applicant_age": 17})
        reply = self.service.post_message(self.user, conversation["conversation_id"], "那签证材料呢", "m2")
        self.assertNotIn("profile_version_changed", [n.code for n in reply.notices])
        self.assertIn("parental_consent", {m.document_code for m in reply.answer.materials
                                           if m.applicability == "required"})

    def test_fixture_adapter_refuses_non_synthetic_records(self):
        records = fixture_records()
        records[0]["synthetic"] = False
        with self.assertRaises(ValueError):
            FixtureKnowledgeRead(records, "test")


if __name__ == "__main__":
    unittest.main()
