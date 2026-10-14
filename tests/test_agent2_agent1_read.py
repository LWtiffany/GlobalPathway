"""M07 接 Agent1 已发布政策：条件映射、真实来源引用、撤回/过期不作确定回答。"""
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import test_publication as base
from agent1 import publication
from agent1.models import VisaPatch
from agent2.agent1_read import Agent1KnowledgeRead, map_condition
from agent2.service import Agent2Service
from agent2.store import Store

UK_STUDENT = {"destination_country": "UK", "visa_category": "student_visa"}
PARENT = "If you're under 18 you'll need written consent from your parents or legal guardian."
ENGLISH = "You must prove your knowledge of English unless you have a degree from a UK university."
TB_ITEMS = ("you're coming to the UK for 6 months or more",
            "you've lived in a listed country for 6 months or more")


def page():
    return (f"# Student visa\n{base.INTRO}\n## London costs\nYou need GBP 1000 per month for courses in London.\n"
            f"## Documents\n{base.CAS}\n{PARENT}\n{ENGLISH}\n### TB test\nYou need a TB test if:\n\n"
            f"- {TB_ITEMS[0]}\n- {TB_ITEMS[1]}\n")


def extraction(section):
    if section.heading == "Documents":
        return VisaPatch.model_validate({"policies": [], "documents": [
            {"code": "cas", "name_zh": "录取确认函（CAS）", "required": True, "evidence_quote": base.CAS},
            {"code": "parental_consent", "name_zh": "父母或法定监护人同意书", "required": True,
             "condition": {"any_of": [{"age": "16 or 17"}, {"under_18": True}]},
             "condition_quote": "If you're under 18", "evidence_quote": PARENT},
            {"code": "english_test_result", "name_zh": "英语能力证明", "required": True,
             "condition": {"no_english_qualification": True},
             "condition_quote": "unless you have a degree from a UK university",
             "evidence_quote": "You must prove your knowledge of English"}]})
    if section.heading == "TB test":
        return VisaPatch.model_validate({"policies": [], "documents": [
            {"code": "tuberculosis_test_certificate", "name_zh": "肺结核检测证明", "required": True,
             "condition": {"coming_to_uk_for_6_months_or_more": True,
                           "lived_in_listed_country_for_6_months_or_more": True},
             "condition_quote": "; ".join(TB_ITEMS), "evidence_quote": "You need a TB test if:"}]})
    return base.extraction(section)


class ConditionMapTests(unittest.TestCase):
    def test_any_of_groups_are_kept_and_synonyms_collapse(self):
        groups = map_condition({"any_of": [{"age": "16 or 17"}, {"under_18": True}]})
        self.assertEqual([[c.op for c in g] for g in groups], [["in"], ["lt"]])
        tb = map_condition({"any_of": [
            {"coming_to_uk_for_6_months_or_more": True, "lived_in_listed_country_for_6_months_or_more": True,
             "living_there_within_last_6_months": True},
            {"coming_for_6_months_or_more": True, "residence_in_listed_country_6_months_or_more": True}]})
        self.assertEqual(len(tb), 1)
        self.assertEqual([c.field for c in tb[0]], ["stay_over_6_months", "tb_listed_country_resident"])

    def test_any_unmapped_key_or_value_is_unknown(self):
        self.assertIsNone(map_condition({"under_18": True, "no_english_qualification": True}))
        self.assertIsNone(map_condition({"under_18": False}))
        self.assertIsNone(map_condition({"any_of": [{"under_18": True}, {"mars_resident": True}]}))


class Agent1PublishedKnowledgeTests(unittest.TestCase):
    def setUp(self):
        base.PublicationFlowTests.setUp(self)
        self.markdown = page()
        extract = patch("agent1.formal_pipeline.formal_extract.extract_section",
                        side_effect=lambda section, *a: (extraction(section), (1, 1)))
        extract.start()
        self.addCleanup(extract.stop)
        base.PublicationFlowTests.scan(self)
        published = base.PublicationFlowTests.publish(self)[0]
        self.assertEqual(published["result"], "published", published)
        self.record_id = published["policy_id"]
        self.knowledge = Agent1KnowledgeRead(self.paths)

    def ask(self, fields, clock=None, knowledge=None):
        store = Store()
        self.addCleanup(store.close)
        now = clock or datetime.now(timezone.utc)
        self.service = Agent2Service(store, knowledge or self.knowledge, clock=lambda: now)
        user = self.service.demo_login("alice")["user_id"]
        profile = self.service.create_profile(user, fields, synthetic=True)
        conversation = self.service.create_conversation(user, profile.profile_id)
        return self.service.post_message(user, conversation["conversation_id"], "需要准备哪些签证材料", "m1")

    def test_answer_cites_published_official_page_and_asks_for_condition_fields(self):
        answer = self.ask(UK_STUDENT)
        self.assertEqual(answer.status, "answered")
        self.assertFalse(answer.synthetic)
        self.assertFalse(any("模拟资料" in limit for limit in answer.answer.limitations))
        by_code = {m.document_code: m for m in answer.answer.materials}
        self.assertEqual(by_code["cas"].applicability, "required")
        self.assertEqual(by_code["english_test_result"].applicability, "unverified")
        self.assertEqual(by_code["parental_consent"].pending_fields, ["applicant_age"])
        self.assertEqual(by_code["tuberculosis_test_certificate"].pending_fields,
                         ["stay_over_6_months", "tb_listed_country_resident"])
        claim = next(c for c in answer.claims if c.requirement_key.startswith("parental_consent"))
        self.assertIn("申请人 16 或 17 岁，或申请人未满 18 岁", claim.text)
        self.assertTrue({"applicant_age", "stay_over_6_months", "tb_listed_country_resident"}
                        & {q.field for q in answer.follow_up_questions})
        for citation in answer.citations:
            self.assertEqual(citation.source_url, "https://example.org/visa")
            self.assertFalse(citation.synthetic)
            snapshot = self.service.knowledge_snapshot(citation.revision_id)
            self.assertIn(citation.quote, [e["quote"] for e in snapshot["evidence"]])

    def test_profile_facts_resolve_conditions(self):
        adult = self.ask({**UK_STUDENT, "applicant_age": 20, "stay_over_6_months": True,
                          "tb_listed_country_resident": True})
        by_code = {m.document_code: m for m in adult.answer.materials}
        self.assertNotIn("parental_consent", by_code)
        self.assertEqual(by_code["tuberculosis_test_certificate"].applicability, "required")

        minor = self.ask({**UK_STUDENT, "applicant_age": 17, "stay_over_6_months": False})
        by_code = {m.document_code: m for m in minor.answer.materials}
        self.assertEqual(by_code["parental_consent"].applicability, "required")
        self.assertNotIn("tuberculosis_test_certificate", by_code)

    def test_stale_or_withdrawn_knowledge_is_never_a_definite_answer(self):
        stale = self.ask(UK_STUDENT, clock=datetime.now(timezone.utc) + timedelta(hours=169))
        self.assertEqual(stale.status, "needs_verification")
        self.assertEqual(stale.answer.materials, [])

        paths = self.paths

        class WithdrawnMidAnswer(Agent1KnowledgeRead):
            def search(self, **kwargs):
                records = super().search(**kwargs)
                publication.withdraw(records[0].record.record_id, actor="reviewer", reason="官方页面更正",
                                     paths=paths)
                return records

        midway = self.ask(UK_STUDENT, knowledge=WithdrawnMidAnswer(self.paths))
        self.assertEqual(midway.status, "cannot_answer")
        self.assertEqual(midway.answer.materials, [])

        gone = self.ask(UK_STUDENT)
        self.assertEqual(gone.status, "cannot_answer")


if __name__ == "__main__":
    unittest.main()
