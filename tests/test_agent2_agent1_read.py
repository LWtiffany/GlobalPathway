"""M07 接 Agent1 已发布政策：条件映射、真实来源引用、撤回/过期不作确定回答。"""
import json
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
UK_STUDENT_READY = {
    **UK_STUDENT,
    "applicant_age": 20,
    "stay_over_6_months": True,
    "tb_listed_country_resident": True,
}
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
    def test_hong_kong_keys_map_by_value(self):
        school, person = (map_condition({"sponsor_is_educational_institution": v})[0][0] for v in (True, False))
        self.assertEqual((school.field, school.value), ("hk_sponsor", "institution"))
        self.assertEqual(person.value, "individual")
        travel = map_condition({"sponsor_is_educational_institution": False,
                                "sponsor_is_hk_permanent_resident": False})[0]
        self.assertEqual([(c.field, c.op) for c in travel],
                         [("hk_sponsor", "eq"), ("hk_sponsor_permanent_resident", "is_false")])
        self.assertEqual(map_condition({"applicant_residence": "Macao SAR"})[0][0].value, "MO")
        self.assertIsNone(map_condition({"applicant_residence": "Mars"}))
        self.assertIsNone(map_condition({"visa_fee_paid_before_interview": True}))

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


class PublishedRegion(unittest.TestCase):
    """A temporary Agent1 environment whose single official page is scanned and published."""
    region = "UK"

    def page(self):
        return page()

    def extraction(self, section):
        return extraction(section)

    def setUp(self):
        base.PublicationFlowTests.setUp(self)
        self.source["region"] = self.region
        self.source_file.write_text(json.dumps([self.source]), encoding="utf-8")
        self.markdown = self.page()
        extract = patch("agent1.formal_pipeline.formal_extract.extract_section",
                        side_effect=lambda section, *a: (self.extraction(section), (1, 1)))
        extract.start()
        self.addCleanup(extract.stop)
        base.PublicationFlowTests.scan(self)
        published = publication.publish(self.region, actor="reviewer", paths=self.paths,
                                        sources=self.sources, ack_warnings=True)[0]
        self.assertEqual(published["result"], "published", published)
        self.record_id = published["policy_id"]
        self.knowledge = Agent1KnowledgeRead(self.paths)

    def ask(self, fields, clock=None, knowledge=None):
        store = Store.temporary()
        self.addCleanup(store.close)
        now = clock or datetime.now(timezone.utc)
        self.service = Agent2Service(store, knowledge or self.knowledge, clock=lambda: now)
        user = self.service.demo_login("alice")["user_id"]
        profile = self.service.create_profile(user, fields, synthetic=True)
        conversation = self.service.create_conversation(user, profile.profile_id)
        return self.service.post_message(user, conversation["conversation_id"], "需要准备哪些签证材料", "m1")

    @staticmethod
    def materials(answer):
        return {m.document_code.split(":")[0]: m for m in answer.answer.materials}


class Agent1PublishedKnowledgeTests(PublishedRegion):
    def test_answer_cites_published_official_page_and_asks_for_condition_fields(self):
        pending = self.ask(UK_STUDENT)
        self.assertEqual(pending.status, "needs_input")
        self.assertTrue({"applicant_age", "stay_over_6_months", "tb_listed_country_resident"}
                        & {q.field for q in pending.follow_up_questions})
        self.assertEqual(pending.answer.materials, [])

        answer = self.ask(UK_STUDENT_READY)
        self.assertEqual(answer.status, "answered")
        self.assertFalse(answer.synthetic)
        self.assertFalse(any("模拟资料" in limit for limit in answer.answer.limitations))
        by_code = {m.document_code: m for m in answer.answer.materials}
        self.assertEqual(by_code["cas"].applicability, "required")
        self.assertEqual(by_code["english_test_result"].applicability, "unverified")
        self.assertNotIn("parental_consent", by_code)
        self.assertEqual(by_code["tuberculosis_test_certificate"].applicability, "required")
        for citation in answer.citations:
            self.assertEqual(citation.source_url, "https://example.org/visa")
            self.assertFalse(citation.synthetic)
            snapshot = self.service.knowledge_snapshot(citation.revision_id)
            self.assertIn(citation.quote, [e["quote"] for e in snapshot["evidence"]])

    def test_profile_facts_resolve_conditions(self):
        adult = self.ask(UK_STUDENT_READY)
        by_code = {m.document_code: m for m in adult.answer.materials}
        self.assertNotIn("parental_consent", by_code)
        self.assertEqual(by_code["tuberculosis_test_certificate"].applicability, "required")

        minor = self.ask({**UK_STUDENT, "applicant_age": 17, "stay_over_6_months": False,
                          "tb_listed_country_resident": False})
        by_code = {m.document_code: m for m in minor.answer.materials}
        self.assertEqual(by_code["parental_consent"].applicability, "required")
        self.assertNotIn("tuberculosis_test_certificate", by_code)

    def test_stale_published_rows_still_answer_withdrawn_does_not(self):
        # Past Agent1 source SLA is not an Agent2 answer gate.
        stale = self.ask(UK_STUDENT_READY, clock=datetime.now(timezone.utc) + timedelta(hours=169))
        self.assertEqual(stale.status, "answered")
        self.assertTrue(stale.answer.materials)

        paths = self.paths

        class WithdrawnMidAnswer(Agent1KnowledgeRead):
            def search(self, **kwargs):
                records = super().search(**kwargs)
                publication.withdraw(records[0].record.record_id, actor="reviewer", reason="官方页面更正",
                                     paths=paths)
                return records

        midway = self.ask(UK_STUDENT_READY, knowledge=WithdrawnMidAnswer(self.paths))
        self.assertEqual(midway.status, "cannot_answer")
        self.assertEqual(midway.answer.materials, [])

        gone = self.ask(UK_STUDENT_READY)
        self.assertEqual(gone.status, "cannot_answer")


HK_PASSPORT = "Copy of the applicant's valid travel document containing personal particulars"
HK_FUNDS = ("Copy of proof of the applicant's financial support, e.g. bank statements "
            "[if the sponsor is the educational institution]")
HK_MACAO = ("Copy of the applicant's Visit Permit for Residents of Macao SAR to Hong Kong SAR and Macao "
            "identity card [for Macao SAR residents only]")
HK_SPONSOR_ID = "Copy of the sponsor's Hong Kong identity card #"
HK_FOOTNOTE = ("# Submission of the documents is not required if the sponsor is the educational institution "
               "granting the acceptance")


class HongKongPublishedKnowledgeTests(PublishedRegion):
    region = "CN-HK"

    def page(self):
        rows = "".join(f"| {row}  |\n" for row in (HK_PASSPORT, HK_FUNDS, HK_MACAO, HK_SPONSOR_ID))
        return (f"# Students\n{base.INTRO}\n## Fees\nThe visa fee is HKD 330 per application.\n"
                f"## Checklist\n| Forms/ Documents Required  |\n| --- |\n{rows}{HK_FOOTNOTE}\n")

    def extraction(self, section):
        if section.heading == "Fees":
            return VisaPatch.model_validate({"policies": [{
                "policy": {"visa_fee": 330},
                "evidence": [{"field": "visa_fee", "evidence_quote": "The visa fee is HKD 330 per application."}]}]})
        if section.heading != "Checklist":
            return VisaPatch(policies=[], documents=[])
        return VisaPatch.model_validate({"policies": [], "documents": [
            {"code": "passport", "name_zh": "护照或有效旅行证件", "required": True, "evidence_quote": HK_PASSPORT},
            {"code": "proof_of_funds", "name_zh": "资金证明", "required": True,
             "condition": {"sponsor_is_educational_institution": True},
             "condition_quote": "[if the sponsor is the educational institution]", "evidence_quote": HK_FUNDS},
            # 截掉了方括号条件的同一材料不能变成无条件必需
            {"code": "proof_of_funds", "name_zh": "资金证明", "required": True,
             "evidence_quote": "Copy of proof of the applicant's financial support"},
            {"code": "other_document", "name_zh": "澳门居民来往香港旅游证及澳门身份证副本", "required": True,
             "condition": {"applicant_residence": "Macao SAR"},
             "condition_quote": "[for Macao SAR residents only]", "evidence_quote": HK_MACAO},
            {"code": "other_document", "name_zh": "保证人香港身份证副本", "required": True,
             "condition": {"sponsor_is_educational_institution": False},
             "condition_quote": HK_FOOTNOTE, "evidence_quote": HK_SPONSOR_ID}]})

    def test_unknown_sponsor_is_asked_and_both_branches_stay_conditional(self):
        pending = self.ask({"destination_country": "CN-HK", "visa_category": "student_visa"})
        self.assertEqual(pending.status, "needs_input")
        self.assertFalse(pending.synthetic)
        self.assertIn("hk_sponsor", {q.field for q in pending.follow_up_questions})
        self.assertEqual(pending.answer.materials, [])
        self.assertTrue(any("immd.gov.hk" in a for a in pending.next_actions))

        answer = self.ask({"destination_country": "CN-HK", "visa_category": "student_visa",
                           "hk_sponsor": "institution", "applicant_residence": "CN"})
        self.assertEqual(answer.status, "answered")
        by_code = self.materials(answer)
        self.assertEqual(by_code["passport"].applicability, "required")
        self.assertEqual(by_code["proof_of_funds"].applicability, "required")
        self.assertEqual({c.source_url for c in answer.citations}, {"https://example.org/visa"})

    def test_institution_sponsor_needs_applicant_funds_not_sponsor_id(self):
        names = lambda answer: {m.name_zh for m in answer.answer.materials}
        school = self.ask({"destination_country": "CN-HK", "visa_category": "student_visa",
                           "hk_sponsor": "institution", "applicant_residence": "CN"})
        self.assertEqual(self.materials(school)["proof_of_funds"].applicability, "required")
        self.assertNotIn("保证人香港身份证副本", names(school))
        self.assertNotIn("澳门居民来往香港旅游证及澳门身份证副本", names(school))

        person = self.ask({"destination_country": "CN-HK", "visa_category": "student_visa",
                           "hk_sponsor": "individual", "applicant_residence": "MO"})
        self.assertNotIn("proof_of_funds", self.materials(person))
        self.assertTrue({"保证人香港身份证副本", "澳门居民来往香港旅游证及澳门身份证副本"} <= names(person))
        self.assertTrue(all(m.applicability == "required" for m in person.answer.materials))


US_ITEMS = ("Your original Form I-20 with ink signature.",
            "Passport valid for travel to the United States.",
            "Form DS-160, Nonimmigrant Visa Application, confirmation page.",
            "Department of State visa application fee payment receipt, if you are required to pay before "
            "your interview.")


class UnitedStatesPublishedKnowledgeTests(PublishedRegion):
    region = "US"

    def page(self):
        items = "".join(f"- {item}\n" for item in US_ITEMS)
        return (f"# Prepare for your visa interview\n{base.INTRO}\n## Fees\n"
                f"The visa application fee is USD 185 per applicant.\n"
                f"## Interview\nYou must bring the following documents to present to the consular officer:\n\n"
                f"{items}")

    def extraction(self, section):
        if section.heading == "Fees":
            return VisaPatch.model_validate({"policies": [{
                "policy": {"visa_fee": 185},
                "evidence": [{"field": "visa_fee",
                              "evidence_quote": "The visa application fee is USD 185 per applicant."}]}]})
        if section.heading != "Interview":
            return VisaPatch(policies=[], documents=[])
        return VisaPatch.model_validate({"policies": [], "documents": [
            {"code": "form_i20", "name_zh": "I-20 表格", "required": True, "evidence_quote": US_ITEMS[0]},
            {"code": "passport", "name_zh": "护照或有效旅行证件", "required": True, "evidence_quote": US_ITEMS[1]},
            {"code": "application_form", "name_zh": "签证申请表", "required": True, "evidence_quote": US_ITEMS[2]},
            {"code": "other_document", "name_zh": "签证申请费缴费收据", "required": True,
             "condition": {"visa_fee_paid_before_interview": True},
             "condition_quote": "if you are required to pay before your interview",
             "evidence_quote": "Department of State visa application fee payment receipt"}]})

    def test_interview_documents_are_required_and_unmapped_fee_condition_is_unverified(self):
        answer = self.ask({"destination_country": "US", "visa_category": "student_visa"})
        self.assertEqual(answer.status, "answered")
        self.assertFalse(answer.synthetic)
        by_code = self.materials(answer)
        self.assertEqual({c for c, m in by_code.items() if m.applicability == "required"},
                         {"form_i20", "passport", "application_form"})
        self.assertEqual(by_code["other_document"].applicability, "unverified")
        self.assertEqual(by_code["application_form"].name_en, "Form DS-160 confirmation page")
        self.assertEqual(answer.follow_up_questions, [])
        self.assertTrue(any("travel.state.gov" in a for a in answer.next_actions))


if __name__ == "__main__":
    unittest.main()
